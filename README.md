# Telegram Digital File Shop (Windows + personal KHQR)

## 1. Create the Telegram bot
1. Open `@BotFather` in Telegram and create a bot with `/newbot`.
2. Copy the bot token privately. If a token has been exposed, revoke it with BotFather and use only the newly generated token.
3. Find your numeric Telegram user ID using a trusted ID bot or your own bot logs.
4. Send `/start` to your new bot once so it can message you.

## 2. Install on Windows
Install Python 3.11 or newer from https://www.python.org/downloads/ and enable **Add Python to PATH**.

Open PowerShell in this folder and run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

If PowerShell blocks activation, you can skip activation and use:
```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 3. Configure secrets and admin
Create a `.env` file beside `bot.py` and enter your values:

```dotenv
BOT_TOKEN=PASTE_YOUR_NEW_BOT_TOKEN_HERE
ADMIN_IDS=YOUR_NUMERIC_TELEGRAM_USER_ID
SHOP_NAME=My Digital Shop
PAYMENT_INSTRUCTIONS=Scan the KHQR and pay the exact amount. Wait for admin verification.
```

For a single-owner setup, put your numeric Telegram user ID in `ADMIN_IDS`. Multiple admin IDs can be comma-separated. The bot loads `.env` automatically; environment variables already set in PowerShell take precedence.

Install dependencies again after the `requirements.txt` update, then start the bot from this folder:

```powershell
py -m pip install -r requirements.txt
py .\bot.py
```

Keep `.env` secret. Do not publish it or commit it to GitHub. If a token has been exposed, revoke it with BotFather and put only the replacement in `.env`.

## 4. Use the bot
- Customers: `/start` → choose a product → **Generate QR**. The **Pay ready** button appears after one minute; tapping it hides the QR and submits the order for admin review. A **Download** button appears after 20 seconds, but only works after an admin verifies the actual payment. Users can optionally send a transaction screenshot for review; it does not confirm payment. **Cancel order & browse products** cancels an unpaid order.
- Customers can tap **Contact owner** in the main menu to open a Telegram chat with [@bun_meng_heng_kh](https://t.me/bun_meng_heng_kh).
- Admin: `/admin` → **Upload product** → send the downloadable file as a Telegram document → send an optional product photo (or `/skipphoto`) → enter product name (up to 256 characters) and description → set the price and choose USD or KHR → send this product's QR as a photo or image document. Each product requires its own QR. The description appears on the product page and in the delivered file caption.
- Admin: `/admin` → **List products** → **Delete product** → confirm. This hides the product from the shop while preserving existing order records.
- Admin reviews a payment in the banking app/account, then uses **Verify & deliver** or **Reject** on the order controls. Verified customers can use the **Download** button to receive their file.
- `/myorders` shows the customer's recent order statuses.
- Admin `/resend ORDER_ID` resends a file for an already-paid order if Telegram delivery failed.

## Important payment safety
A KHQR image does not give this bot a trusted transaction-status API. This project deliberately does not approve payments based on screenshots, user messages, or a button click by the customer. Admins must verify the actual incoming transaction and amount in the banking app before clicking **Verify & deliver**.

## Notes / limitations
- This is a polling bot intended to run while your Windows PC is on and connected to the internet.
- If startup logs a Telegram connection timeout, the bot now retries startup automatically. If it keeps retrying, check your internet connection and whether your network/firewall can reach `api.telegram.org`.
- SQLite data is stored in `shop.db` in the working folder; back it up.
- Telegram file IDs are stored for product files. Keep the bot account intact.
- Use only files you have rights to sell, and clearly state your refund/support policy.
