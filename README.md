Gang Bot 🤖

Gang Bot is an AI-powered Telegram bot designed to manage group finances and logistics. Built with Python and powered by Google Gemini, it understands natural language to track expenses, calculate debts, and schedule meetups without rigid command structures.

✨ Key Features

Natural Language Processing: Just talk to the bot like a human (e.g., "I paid 50 for dinner with Alice").

Smart Ledger: Automatically calculates splits, debts, and repayments.

Context Awareness: Remembers the context of conversations (e.g., if you forget to say how much you spent, it asks, and you can just reply with the number).

Scheduling: Interactive polls to find common availability.

Google Sheets Backend: All data is stored in a transparent, editable Google Sheet.

🚀 Usage Guide

You can interact with Gang Bot using Commands (/command) or Natural Language by tagging the bot (@BotName).

1. 💸 Expense Tracking

Log shared costs easily. The bot distinguishes between splitting costs ("with") and paying on behalf of someone ("for").

Command: /spent [amount] [description] [people]

Natural Language:

"@GangBot I spent $50 on Pizza" (Splits with everyone)

"@GangBot Lunch with @Alice was 30" (Splits between You and Alice)

"@GangBot Bought tickets for @Bob costs 100" (Bob owes you 100)

Example:

You: /spent 60 Sushi @Alice @Bob
Bot: ✅ Expense Logged! Amount: $60, For: Sushi, People: @Alice, @Bob, You

2. 💰 Payments & Settlements

Log when you pay someone back to reduce your debt.

Command: /paid [amount] [recipient]

Natural Language:

"@GangBot I paid @Alice 20 dollars"

"@GangBot returned 15 to @Bob"

Example:

You: @GangBot I paid @Alice 25
Bot: 💸 Payment Logged! Amount: $25, To: @Alice

3. 📊 Checking Balances

See who owes whom in real-time.

Command: /mybalance

Natural Language:

"@GangBot what is my balance?"

"@GangBot how much does @Alice owe?"

Output:
The bot displays the full group ledger:

📊 Group Ledger:
@Alice: 🔴 owes $15.00
@Bob: 🟢 is owed $15.00

4. 🤝 Settle Up Advice

Not sure exactly how much to pay? Ask the bot.

Command: /settleup [user]

Natural Language:

"@GangBot settle up with @Alice"

Output:

📉 Settlement Helper
Your Balance: -$10.00
@Alice's Balance: +$10.00
💡 To pay @Alice, type: /paid 10 @Alice

5. 🗓 Scheduling

Create interactive voting polls for meetups.

Command: /meetup [date1], [date2], ...

Example:

You: /meetup Fri, Sat, Sun
Bot: Sends a message with interactive buttons. Users tap to check/uncheck their availability. The buttons update in real-time to show who is free.

🛠 Admin & System Commands

/start - Wakes up the bot and registers you in the database.

/help - Shows a quick list of commands.

/ledger - Displays the last 5 transactions from the sheet.

/broadcast [message] - Sends a pinned announcement to the group.

/clear_debts - Adds a "RESET" row to the spreadsheet (starts balances from zero).

⚙️ Technical Setup

Prerequisites

Python 3.9+

A Telegram Bot Token (via @BotFather)

Google Gemini API Key

Google Cloud Service Account (for Sheets API)

Installation

Clone the repository:

git clone [https://github.com/YourUsername/gang-bot.git](https://github.com/YourUsername/gang-bot.git)
cd gang-bot


Install dependencies:

pip install -r requirements.txt


Configure Environment:
Create a .env file in the root directory:

TELEGRAM_BOT_TOKEN=your_token_here
GEMINI_API_KEY=your_gemini_key_here
SHEET_NAME=Name of your Google Sheet


Google Sheets Setup:

Create a Google Sheet with two tabs: Expenses and Users.

Share the sheet with your Service Account email (Editor access).

Place your credentials.json file in the project root.

Run the Bot:

python gang_bot.py


🔒 Security

This project uses .gitignore to ensure API keys (.env) and credentials (credentials.json) are never uploaded to the repository.

Enjoy your organized friend group! 🎉