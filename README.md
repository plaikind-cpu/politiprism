# PolitiPrism

Political statement tracker and fact-checker. Monitors configurable politicians daily,
extracts verifiable claims from news coverage, and fact-checks each one using
Brave Search + Claude AI.

## Tech Stack
- Flask + SQLite (Railway)
- Brave Search News API (ingestion + evidence)
- Claude claude-sonnet-4-20250514 (claim extraction + verdict)
- APScheduler (daily cron at 7 AM UTC)
- Magic-link auth, invitation-only

## Deployment (Railway)

### 1. Create GitHub repo
- Repo name: `PolitiPrism`
- Push all files

### 2. Create Railway project
- New project → Deploy from GitHub repo → select PolitiPrism
- Railway auto-detects Procfile

### 3. Set environment variables in Railway dashboard
```
ANTHROPIC_API_KEY=your_key_here
BRAVE_API_KEY=your_key_here
SECRET_KEY=any_long_random_string
ADMIN_EMAIL=paul@pklmedialab.com
DB_PATH=/tmp/politiprism.db
```

### 4. Deploy and open
- Railway gives you a URL like: politiprism-production.up.railway.app
- Navigate to the URL — DB initializes automatically with 3 default politicians
- Log in with your admin email → get magic link from Railway logs
- Go to Admin → Run Pipeline Now to test immediately

## Usage

### Adding politicians
Admin panel → Tracked Politicians → fill in Name, Role, Search Terms
- Search terms are comma-separated Brave News queries
- Example: `Biden said, Biden claims, Biden announced`

### Daily digest
- Auto-runs at 7 AM UTC daily
- Browse by date using the date picker
- Color-coded verdicts: green=TRUE, red=FALSE, amber=MISLEADING, gray=UNVERIFIABLE

### Inviting users
Admin panel → Invited Users → add email
- They enter their email at login, get a magic link (appears in Railway logs until email infra is added)

## Notes
- SQLite resets on Railway redeploy — this is acceptable for MVP
- Add email sending (SendGrid/ImprovMX) later if needed for magic links
- Custom domain: add in Railway dashboard → Settings → Domains
