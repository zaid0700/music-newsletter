# Weekly Music Release Newsletter

Reads music feeds every day, picks out the week's major releases in four genres
(Pop, Hip-hop/R&B, Rock/Alt/Indie, Electronic/Dance), ranks them by critical buzz
and artist popularity, and emails a newsletter every Friday at 8:00 AM UAE time.

---

## Step 1: Set up GitHub (one-time, about 15 minutes)

GitHub stores the code and runs it on a schedule for free, so nothing needs to run
on your own computer.

### 1a. Create an account
1. Go to https://github.com/signup and create an account with your **personal** email.
2. Turn on two-factor authentication when prompted (Settings → Password and authentication).

### 1b. Create the repository
1. Click the **+** in the top-right corner → **New repository**.
2. Name: `music-newsletter`
3. Visibility: **Private**.
4. Leave everything else unticked and click **Create repository**.

### 1c. Upload the project files
1. On the new repository page, click **uploading an existing file**.
2. Unzip `music-newsletter.zip` on your computer.
3. Drag the **contents** of the unzipped folder (the `config` and `scripts` folders,
   `README.md`, `requirements.txt`) into the upload area.
4. Click **Commit changes**.

> The `.github` and `.gitignore` items are hidden files on Mac/Windows. Skip them for
> now; we'll add the workflow file directly in GitHub in a later step.

### 1d. Where your keys will go (later steps)
Passwords and API keys never go in the code. They go in
**Settings → Secrets and variables → Actions → New repository secret**.
We'll add these as we reach each step:

| Secret name          | What it is                                   | Added in |
|----------------------|----------------------------------------------|----------|
| `ANTHROPIC_API_KEY`  | Key for the Claude API (finds & summarises releases) | Step 4 |
| `LASTFM_API_KEY`     | Free key for artist popularity                | Step 6 |
| `GMAIL_ADDRESS`      | The Gmail account that sends the newsletter  | Step 8 |
| `GMAIL_APP_PASSWORD` | A Gmail app password (not your normal password) | Step 8 |
| `NEWSLETTER_TO`      | The address that receives it                  | Step 8 |

---

## Step 2: Check the feeds

The feed list lives in `config/sources.yaml`. To confirm each one works, run the
checker on your computer (needs Python 3.10+):

```bash
cd music-newsletter
pip install -r requirements.txt
python scripts/check_feeds.py
```

Each feed prints `OK` with its newest item, or `FAIL` with the reason.
A failing feed can simply be removed from `sources.yaml`.

---

## Project layout

```
music-newsletter/
├── config/sources.yaml     ← feeds, weights and genres (edit freely)
├── scripts/check_feeds.py  ← Step 2 feed checker
├── requirements.txt        ← Python libraries
└── README.md
```

Later steps add: `collect.py` (daily), `analyse.py` + `send.py` (Friday),
an email template, and the GitHub Actions schedule.
