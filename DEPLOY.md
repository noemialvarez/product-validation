# Deployment runbook

Goal: get this app live at `https://product-validation.insightsphere.co`
with reports stored in Notion. Total cost: **$0/month**.

Stack:
- **Render** (free tier) — runs the Streamlit app, supports custom domain + free SSL
- **Notion** — database for saved reports
- **Hostpoint** — DNS only (just add one CNAME record)

⚠️ Cold-start tradeoff on Render's free tier: the app sleeps after 15 minutes of
inactivity. The next visitor waits ~30 seconds while it wakes up. Subsequent
visits are instant.

---

## Step 1 — Create the Notion database

1. Open Notion → create a new page → type `/database` → choose **Database — Full page**.
2. Name it something like **Product Validation Reports**.
3. Set up these exact properties (case matters):
   | Property name | Type |
   |---|---|
   | Product | Title (the default — rename it from "Name" to "Product") |
   | Date | Date |
   | Web Search | Checkbox |
   | Description | Text |

4. **Copy the database ID:**
   - Click `Share` → `Copy link`. The URL looks like:
     ```
     https://www.notion.so/<workspace>/<DATABASE_ID>?v=<view_id>
     ```
   - The `<DATABASE_ID>` is a 32-character hex string. Save it.

## Step 2 — Create the Notion integration + connect it to the DB

1. Go to https://www.notion.so/profile/integrations
2. Click **+ New integration**:
   - Name: `Product Validation App`
   - Associated workspace: yours
   - Click `Save`
3. Copy the **Internal Integration Secret** (starts with `secret_` or `ntn_`). Save it.
4. Go back to your database in Notion. Click the `···` menu (top-right) → `Connections` → search for `Product Validation App` → click to connect.

You now have two values to use later as Render env vars:
- `NOTION_TOKEN` = the secret you copied
- `NOTION_DATABASE_ID` = the 32-char database ID

## Step 3 — Push the code to GitHub

If you don't have a GitHub account: sign up at https://github.com.

From WSL, in this project folder:
```bash
cd ~/claude-projects/test/product-validation-agent

# First-time git setup (if you haven't done this on this machine before)
git config --global user.name "Your Name"
git config --global user.email "your@email.com"

git init
git add .
git commit -m "Initial commit: Product Validation app"
```

Then on GitHub:
1. Create a new repository named `product-validation` (public is fine; the secrets are in env vars, not code).
2. Follow the "push an existing repository" instructions GitHub shows. Roughly:
   ```bash
   git branch -M main
   git remote add origin https://github.com/<your-username>/product-validation.git
   git push -u origin main
   ```

## Step 4 — Deploy to Render

1. Sign up at https://render.com (use the same email as GitHub for easy connection).
2. Dashboard → **New +** → **Web Service**.
3. Connect your GitHub account, then pick the `product-validation` repo.
4. Configure:
   - **Name:** `product-validation` (this becomes part of the default URL)
   - **Region:** `Frankfurt` (closest to Switzerland)
   - **Branch:** `main`
   - **Runtime:** `Python 3`
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:**
     ```
     streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true
     ```
   - **Instance type:** `Free`
5. Click **Advanced** and add Environment Variables:
   | Key | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | your real Anthropic key |
   | `APP_PASSWORD` | a strong password — this is what users type to enter the app |
   | `NOTION_TOKEN` | from Step 2 |
   | `NOTION_DATABASE_ID` | from Step 1 |
   | `PYTHON_VERSION` | `3.12.5` |
6. Click **Create Web Service**.

First build takes 5–10 minutes. When done, Render gives you a URL like
`https://product-validation.onrender.com`. Open it and confirm:
- The login form appears
- Your password works
- Running an evaluation creates a new row in your Notion database

## Step 5 — Add the custom domain (`product-validation.insightsphere.co`)

**On Render:**
1. In your service, go to `Settings` → `Custom Domains` → `+ Add Custom Domain`.
2. Enter `product-validation.insightsphere.co` → `Save`.
3. Render shows you a value to add as a CNAME — something like
   `product-validation-xxxx.onrender.com`. Copy that.

**On Hostpoint:**
1. Log in to your Hostpoint Control Panel.
2. Navigate to your domain `insightsphere.co` → `DNS-Editor` (or `DNS Settings`).
3. Add a new record:
   - **Type:** `CNAME`
   - **Name / Host:** `product-validation`
   - **Value / Target:** the Render hostname from above
   - **TTL:** default (3600 or whatever Hostpoint suggests)
4. Save.

DNS propagation usually takes 5–30 minutes (occasionally longer). Render will
automatically issue a Let's Encrypt SSL certificate once it sees the DNS pointing
correctly. The domain page in Render will say "Certificate Issued" when ready.

## Step 6 — Test

Open `https://product-validation.insightsphere.co`:
- ✅ Loads (may take 30s if cold)
- ✅ Sign-in screen → password works
- ✅ Run an evaluation → reports appear in your Notion DB

---

## Troubleshooting

- **"Application failed to respond" / build fails:** check the Render `Logs` tab.
  Most common cause: a typo in start command or a missing env var.
- **Notion save fails silently:** check the Render logs for `Notion save failed`.
  Most common cause: the integration isn't connected to the database (Step 2.4),
  or property names don't match exactly (case-sensitive: `Product`, `Date`,
  `Web Search`, `Description`).
- **Cold-start hurts too much:** upgrade Render to the $7/mo Starter plan
  (Dashboard → Settings → Change instance type → Starter). No code changes needed.
- **Reports not appearing after restart:** confirm `NOTION_TOKEN` and
  `NOTION_DATABASE_ID` are set on Render and that the database is shared with
  the integration.

## Rotating the Anthropic key later

If you ever need to rotate the Anthropic key:
1. Go to https://console.anthropic.com/settings/keys
2. Delete the old key, create a new one
3. On Render → Service → `Environment` → edit `ANTHROPIC_API_KEY` → Save
4. Render auto-redeploys with the new key
