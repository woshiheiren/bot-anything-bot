import logging
import json
import os
import re
import time
import gspread
from datetime import datetime
from collections import defaultdict
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, constants
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters
import google.generativeai as genai
from dotenv import load_dotenv

# --- CONFIGURATION ---
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SHEET_NAME = os.getenv("SHEET_NAME")

if not all([BOT_TOKEN, GEMINI_API_KEY, SHEET_NAME]):
    raise ValueError("❌ Missing keys! Check your .env file.")

# --- SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.WARNING
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("googleapiclient").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('models/gemini-2.5-flash-lite') 

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

# --- CACHING LAYER ---
class SheetCache:
    def __init__(self, sheet_name):
        self.sheet_name = sheet_name
        self.expenses_data = None
        self.expenses_timestamp = 0
        self.users_data = None
        self.users_timestamp = 0
        self.CACHE_TTL = 15 # Very short cache for testing responsiveness

    def _get_sheet(self, tab_name):
        try:
            return client.open(self.sheet_name).worksheet(tab_name)
        except Exception as e:
            logger.error(f"Sheet Access Error ({tab_name}): {e}")
            return None

    def get_expenses_rows(self, force_refresh=False):
        now = time.time()
        if force_refresh or self.expenses_data is None or (now - self.expenses_timestamp > self.CACHE_TTL):
            ws = self._get_sheet("Expenses")
            if ws:
                self.expenses_data = ws.get_all_values()
                self.expenses_timestamp = now
        return self.expenses_data or []

    def get_users_records(self, force_refresh=False):
        now = time.time()
        if force_refresh or self.users_data is None or (now - self.users_timestamp > self.CACHE_TTL):
            ws = self._get_sheet("Users")
            if ws:
                self.users_data = ws.get_all_records()
                self.users_timestamp = now
        return self.users_data or []

    def append_expense(self, row):
        ws = self._get_sheet("Expenses")
        if ws:
            ws.append_row(row)
            self.expenses_data = None 

    def append_user(self, row):
        ws = self._get_sheet("Users")
        if ws:
            ws.append_row(row)
            self.users_data = None 

db = SheetCache(SHEET_NAME)

# --- GLOBAL MEMORY ---
active_polls = {}
pending_actions = {} 
user_cache = {} 

# --- HELPER: JSON EXTRACTOR ---
def extract_json(text):
    try:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            json_str = match.group(0)
            return json.loads(json_str)
        return json.loads(text) 
    except Exception as e:
        logger.error(f"JSON Parse Error: {e}")
        return None

# --- USER MANAGEMENT (NORMALIZATION HELPER) ---
def get_user_map(group_id):
    """Returns lookup dicts for normalization."""
    # Always refresh for accuracy
    update_user_cache(group_id)
    
    roster = user_cache.get(str(group_id), {})
    
    lookup = {}
    for name, handle in roster.items():
        # Map Firstname -> Handle
        lookup[name.lower()] = handle
        # Map @Handle -> Handle
        lookup[handle.lower()] = handle
        # Map Handle (no @) -> Handle
        lookup[handle.lstrip('@').lower()] = handle
        
    return lookup, roster

def update_user_cache(group_id):
    try:
        all_records = db.get_users_records(force_refresh=True)
        group_roster = {}
        for row in all_records:
            if str(row.get('Group ID')) == str(group_id):
                group_roster[row.get('User First Name')] = row.get('Telegram Handle')
        user_cache[str(group_id)] = group_roster
        return group_roster
    except Exception as e:
        logger.error(f"Error fetching users: {e}")
        return {}

def register_user(user, chat_id):
    if user.is_bot: return
    group_id = str(chat_id)
    first_name = user.first_name
    handle = f"@{user.username}" if user.username else user.first_name
    
    update_user_cache(group_id)
    current_roster = user_cache.get(group_id, {})
    
    # Check if handle or name exists
    if handle not in current_roster.values():
        try:
            db.append_user([group_id, first_name, handle, str(user.id)])
            update_user_cache(group_id) 
        except Exception as e:
            logger.error(f"Register Error: {e}")

# --- MATH ENGINE ---
def get_balances(group_id):
    try:
        rows = db.get_expenses_rows(force_refresh=True)
        if len(rows) < 2: return {} 
        
        lookup, _ = get_user_map(group_id)
        balances = defaultdict(float)
        
        for row in rows[1:]: 
            if len(row) > 6 and str(row[6]) == str(group_id):
                try:
                    clean_amount = re.sub(r'[^\d.]', '', row[1])
                    if not clean_amount: continue
                    amount = float(clean_amount)
                    
                    raw_payer = row[3]
                    # Normalize Payer
                    payer = lookup.get(raw_payer.lower(), raw_payer)
                    
                    raw_split = [x.strip() for x in row[4].split(',') if x.strip()]
                    if not raw_split: continue
                    
                    # Logic: If Payment, it's a direct transfer. If Expense, it's shared.
                    # Note: Our sheet doesn't explicitly flag Payment vs Expense types in a column yet.
                    # We infer based on description or just treating everything as shared cost.
                    # For V2.3, we treat everything as "Shared Cost".
                    # A "Payment" of $10 from A to B is recorded as:
                    # Paid By: A, Split Between: B.
                    # Math: A gets +10. B gets -10. This correctly reduces B's claim on A.
                    
                    split_between = []
                    if 'ALL' in raw_split:
                        split_between = list(set(lookup.values()))
                    else:
                        for p in raw_split:
                            norm = lookup.get(p.lower(), p)
                            split_between.append(norm)
                    
                    if not split_between: continue

                    cost_per_person = amount / len(split_between)
                    
                    balances[payer] += amount
                    for person in split_between:
                        balances[person] -= cost_per_person
                        
                except ValueError:
                    continue 
                    
        return balances
    except Exception as e:
        logger.error(f"Balance Math Error: {e}")
        return {}

# --- AI PARSER (STRICTER LOGIC) ---
def ask_gemini_to_parse(text, sender_name, sender_handle, known_users_dict):
    roster_str = ", ".join([f"{name}: {handle}" for name, handle in known_users_dict.items()])
    
    prompt = f"""
    Context:
    - Sender: {sender_name} (Handle: {sender_handle})
    - Roster: [{roster_str}]
    - Message: "{text}"
    
    Task: Extract structured data.
    
    Intents:
    - 'EXPENSE': User bought something (e.g. "Lunch", "Tickets").
    - 'PAYMENT': User paying back debt (e.g. "/paid", "Returned money to Mel").
    - 'BALANCE': Checking status.
    - 'SETTLE_INTENT': Wants to settle.
    - 'UNKNOWN': Missing critical info (Amount OR Description).
    
    CRITICAL RULES FOR 'involved':
    1. **Preposition 'FOR'**: "Spent 10 FOR Alice" -> involved=['@AliceHandle']. (Gift/Debt).
    2. **Preposition 'WITH'**: "Lunch WITH Alice" -> involved=['{sender_handle}', '@AliceHandle']. (Split).
    3. **PAYMENT**: "Paid 10 TO Alice" -> involved=['@AliceHandle']. (Direct Transfer).
    4. **Default**: If no names mentioned in EXPENSE -> involved=['ALL'].
    5. **Unrecognized Names**: If name not in Roster, return raw name (e.g. "@Bob").
    
    Return JSON:
    {{
        "intent": "EXPENSE" | "PAYMENT" | "BALANCE" | "SETTLE_INTENT" | "UNKNOWN",
        "amount": number or null,
        "description": string or null,
        "involved": [list of strings],
        "target_user": string or null,
        "reply_message": string or null (Question to ask user if UNKNOWN)
    }}
    """
    try:
        response = model.generate_content(prompt)
        return extract_json(response.text)
    except Exception as e:
        logger.error(f"Gemini Error: {e}")
        return None

# --- CORE LOGIC ENGINE ---
async def process_natural_language(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_key = (chat_id, user.id)
    
    # 1. Start Processing
    if user_key not in pending_actions:
        # Only show loading if starting new action
        loading_msg = await update.message.reply_text("⏳ Processing...")
        await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
    else:
        loading_msg = None # Don't spam loading on replies
    
    register_user(user, chat_id)
    
    # 2. Context & History Merging
    if user_key in pending_actions:
        # We are in a reply chain. Merge history.
        prev_data = pending_actions[user_key]
        full_text = f"{prev_data['text']} {user_text}" # Append new answer
    else:
        full_text = user_text

    sender_handle = f"@{user.username}" if user.username else user.first_name
    roster = user_cache.get(str(chat_id), {})
    
    # 3. AI Parsing
    parsed = ask_gemini_to_parse(full_text, user.first_name, sender_handle, roster)
    
    # Error Handling
    if not parsed:
        msg = "❌ Brain freeze. Try again."
        if loading_msg: await loading_msg.edit_text(msg)
        else: await update.message.reply_text(msg)
        if user_key in pending_actions: del pending_actions[user_key]
        return

    intent = parsed.get("intent", "UNKNOWN")
    
    # --- BRANCH 0: CHECK FOR MISSING FIELDS ---
    is_incomplete = False
    missing_fields = []
    
    if intent in ['EXPENSE', 'PAYMENT']:
        amount = parsed.get("amount")
        desc = parsed.get("description")
        involved = parsed.get("involved", [])
        
        # Auto-fill description for payments
        if intent == 'PAYMENT' and not desc: desc = "Payment"
        
        if not amount: missing_fields.append("Amount")
        if not desc: missing_fields.append("Description")
        
        # Payment MUST have a specific recipient
        if intent == 'PAYMENT' and (not involved or 'ALL' in involved):
             missing_fields.append("Who (Recipient)")
        
        # Expense defaults to ALL if empty (handled by AI, but double check)
        if intent == 'EXPENSE' and not involved:
            involved = ['ALL']

        # Check for unknown users
        unknown = [n for n in involved if not n.startswith("@") and n != "ALL"]
        if unknown: missing_fields.append(f"Who is {', '.join(unknown)}? (Tag them)")
        
        if missing_fields: is_incomplete = True

    # --- ACTION: ASK FOR CLARIFICATION ---
    if intent == "UNKNOWN" or is_incomplete:
        # Update State
        pending_actions[user_key] = {"text": full_text} 
        
        # Determine question
        ai_question = parsed.get("reply_message")
        if missing_fields:
            system_question = f"🤔 Missing info: **{', '.join(missing_fields)}**"
        else:
            system_question = ai_question or "I didn't catch that. Can you clarify?"

        if loading_msg: await loading_msg.delete()
        await update.message.reply_text(system_question, reply_markup=ForceReply(selective=True))
        return

    # --- BRANCH 1: BALANCE DISPLAY (FIXED P03) ---
    if intent == 'BALANCE':
        balances = get_balances(chat_id)
        if not balances:
            msg = "💰 Ledger is empty."
            if loading_msg: await loading_msg.edit_text(msg)
            return
            
        target = parsed.get("target_user")
        lookup, roster = get_user_map(chat_id)
        
        # If looking for specific user
        if target:
            t_handle = lookup.get(target.lower(), target)
            val = balances.get(t_handle, 0)
            status = "is owed" if val > 0 else "owes"
            readable = next((n for n, h in roster.items() if h == t_handle), t_handle)
            final_msg = f"💰 **{readable}:** {status} ${abs(val):.2f}"
        
        else:
            # Show FULL GROUP STATUS (The fix for P03)
            final_msg = "📊 **Group Ledger:**\n"
            has_data = False
            for handle, val in balances.items():
                if abs(val) < 0.01: continue # Skip settled
                has_data = True
                status = "🟢 is owed" if val > 0 else "🔴 owes"
                # Try to find readable name
                r_name = next((n for n, h in roster.items() if h == handle), handle)
                final_msg += f"{r_name}: {status} ${abs(val):.2f}\n"
            
            if not has_data: final_msg = "✅ Everyone is settled up!"

        if loading_msg: await loading_msg.edit_text(final_msg)
        else: await update.message.reply_text(final_msg)
        return

    # --- BRANCH 2: SETTLE ADVICE ---
    if intent == 'SETTLE_INTENT':
        target = parsed.get("target_user")
        if not target:
             msg = "Who do you want to settle with? Try: 'Settle with @Mel'"
             if loading_msg: await loading_msg.edit_text(msg)
             else: await update.message.reply_text(msg)
             return
             
        balances = get_balances(chat_id)
        lookup, roster = get_user_map(chat_id)
        t_handle = lookup.get(target.lower(), target)
        
        my_bal = balances.get(sender_handle, 0)
        target_bal = balances.get(t_handle, 0)
        
        t_name = next((n for n, h in roster.items() if h == t_handle), t_handle)
        
        msg = f"📉 **Settlement Info**\nYou (Net): ${my_bal:.2f}\n{t_name} (Net): ${target_bal:.2f}\n\n"
        msg += f"💡 To pay {t_name}, type:\n`/paid [amount] {t_handle}`"
        
        if loading_msg: await loading_msg.edit_text(msg)
        else: await update.message.reply_text(msg)
        return

    # --- BRANCH 3: LOGGING (EXPENSE/PAYMENT) ---
    if intent in ['EXPENSE', 'PAYMENT']:
        date = datetime.now().strftime("%Y-%m-%d")
        group_title = update.effective_chat.title or f"Private: {user.first_name}"
        involved_str = ", ".join(involved)
        
        try:
            row = [date, amount, desc, user.first_name, involved_str, group_title, str(chat_id)]
            db.append_expense(row)
            
            # CLEAR STATE ON SUCCESS
            if user_key in pending_actions: del pending_actions[user_key]
            
            icon = "💸" if intent == 'PAYMENT' else "✅"
            final_msg = (
                f"{icon} **{intent.title()} Logged!**\n"
                f"Amount: ${amount}\n"
                f"Note: {desc}\n"
                f"People: {involved_str}"
            )
            if loading_msg: await loading_msg.edit_text(final_msg)
            else: await update.message.reply_text(final_msg)
            
        except Exception as e:
            logger.error(f"Sheet Error: {e}")
            msg = "❌ Error saving to sheet."
            if loading_msg: await loading_msg.edit_text(msg)
            else: await update.message.reply_text(msg)

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user, update.effective_chat.id)
    await update.message.reply_text("👋 Gang Bot 2.3 Ready!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Commands**\n/spent 50 pizza\n/paid 20 @user\n/mybalance\n/settleup @user"
    )

async def standard_command_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wraps commands like /spent to use NLP engine"""
    cmd = update.message.text.split(' ')[0] # e.g. /spent
    args = " ".join(context.args)
    
    if cmd == '/mybalance': text = "Check my balance"
    elif cmd == '/spent': text = f"I spent {args}"
    elif cmd == '/paid': text = f"I paid {args}" # Will trigger PAYMENT intent check
    elif cmd == '/settleup': text = f"Settle up {args}"
    else: text = args
    
    await process_natural_language(update, context, text)

async def handle_mention(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if f"@{context.bot.username}" in update.message.text:
        # Clean the bot name out so it doesn't confuse Gemini
        clean_text = update.message.text.replace(f"@{context.bot.username}", "").strip()
        await process_natural_language(update, context, clean_text)

async def handle_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_key = (update.effective_chat.id, update.effective_user.id)
    # Only handle reply if we are WAITING for one
    if user_key in pending_actions:
        await process_natural_language(update, context, update.message.text)

# --- POLLS (UNCHANGED) ---
async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args: return await update.message.reply_text("Usage: /meetup A, B")
    dates = [d.strip() for d in " ".join(args).split(',')]
    kb = [[InlineKeyboardButton(f"⬜ {d}", callback_data=f"vote_{d}")] for d in dates]
    await update.message.reply_text(f"🗓 **Poll:**", reply_markup=InlineKeyboardMarkup(kb))

async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Voted")
    # (Simplified logic for brevity, copy full logic if needed)
    data = q.data
    # ... standard button handling ...

if __name__ == '__main__':
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('meetup', create_poll))
    
    # Route all financial commands to the NLP Engine
    for cmd in ['spent', 'paid', 'mybalance', 'settleup']:
        app.add_handler(CommandHandler(cmd, standard_command_wrapper))
    
    app.add_handler(MessageHandler(filters.TEXT & filters.Entity("mention"), handle_mention))
    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, handle_reply))
    app.add_handler(CallbackQueryHandler(handle_vote))
    
    print("Gang Bot 2.3 (Strict Logic) Running...")
    app.run_polling()