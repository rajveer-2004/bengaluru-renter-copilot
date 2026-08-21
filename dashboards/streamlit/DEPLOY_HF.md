# Deploy the Streamlit dashboard to Hugging Face Spaces

Free public hosting for the Streamlit app. Takes ~15 minutes for the first
deploy, ~30 seconds for updates.

## Prerequisites

- A Hugging Face account: https://huggingface.co/join
- A **User Access Token** with write scope:
  https://huggingface.co/settings/tokens → "New token" → **write** role

## One-time setup

### 1. Create a new Space on HF

Go to https://huggingface.co/new-space and fill in:

- **Owner**: your username
- **Space name**: `bengaluru-renter-copilot` (or whatever)
- **License**: MIT
- **SDK**: **Streamlit**
- **Hardware**: CPU basic (free)
- **Visibility**: Public

Click **Create Space**. HF gives you a git URL like
`https://huggingface.co/spaces/<you>/bengaluru-renter-copilot`.

### 2. Clone the Space repo locally

```powershell
cd C:\coding
git clone https://huggingface.co/spaces/<you>/bengaluru-renter-copilot hf-space
cd hf-space
```

When it prompts for credentials, use your HF username + the write-scope token
you generated above (as the password).

### 3. Copy the app + data into it

```powershell
# From C:\coding\bengaluru-renter-copilot
Copy-Item dashboards\streamlit\app.py           C:\coding\hf-space\app.py
Copy-Item dashboards\streamlit\requirements.txt C:\coding\hf-space\requirements.txt
New-Item -ItemType Directory -Force -Path C:\coding\hf-space\db,C:\coding\hf-space\pricing
Copy-Item db\copilot.db                          C:\coding\hf-space\db\copilot.db
Copy-Item pricing\xgb-v1.pkl                     C:\coding\hf-space\pricing\
Copy-Item pricing\xgb-v1.features.json           C:\coding\hf-space\pricing\
```

### 4. Fix the paths in the copied app.py

The Streamlit app uses `Path(__file__).resolve().parents[2]` to find the DB.
That works locally where the app lives at `dashboards/streamlit/app.py`. On
HF the app is at the repo root, so `parents[2]` points off the tree.

Open `C:\coding\hf-space\app.py`, find:

```python
REPO_ROOT   = Path(__file__).resolve().parents[2]
```

Replace with:

```python
REPO_ROOT   = Path(__file__).resolve().parent
```

### 5. Push to HF

```powershell
cd C:\coding\hf-space
git add .
git commit -m "initial deploy: streamlit + snapshot db + xgb-v1 model"
git push
```

HF starts building. In ~2-3 min your Space is live at
`https://huggingface.co/spaces/<you>/bengaluru-renter-copilot`.

## Updating with fresh data

Every time you re-scrape and want the public dashboard to reflect new deals:

```powershell
Copy-Item db\copilot.db          C:\coding\hf-space\db\copilot.db
Copy-Item pricing\xgb-v1.pkl     C:\coding\hf-space\pricing\
Copy-Item pricing\xgb-v1.features.json C:\coding\hf-space\pricing\

cd C:\coding\hf-space
git add db/copilot.db pricing/
git commit -m "data refresh $(Get-Date -Format 'yyyy-MM-dd')"
git push
```

HF rebuilds the Space in ~30 seconds. Refresh the browser.

## Automating the refresh

The `.github/workflows/weekly.yml` cron already pushes updated data to a
`data-snapshot` branch on GitHub. To auto-sync that to HF, add a second
GitHub Actions step in weekly.yml that pushes to the HF remote too:

```yaml
- name: Push to HF Space
  env:
    HF_TOKEN: ${{ secrets.HF_TOKEN }}
  run: |
    git remote add hf https://user:${HF_TOKEN}@huggingface.co/spaces/<you>/bengaluru-renter-copilot
    # Rewrite paths (Streamlit expects root-level db/ + pricing/)
    mkdir -p _hf_snapshot/db _hf_snapshot/pricing
    cp dashboards/streamlit/app.py _hf_snapshot/app.py
    cp dashboards/streamlit/requirements.txt _hf_snapshot/requirements.txt
    cp db/copilot.db _hf_snapshot/db/
    cp pricing/xgb-v1.pkl _hf_snapshot/pricing/
    cp pricing/xgb-v1.features.json _hf_snapshot/pricing/
    # ... commit and push logic ...
```

Skip that for MVP — manual `Copy-Item` + `git push` is fine.

## Once live

Add the URL to your GitHub README top-line, LinkedIn projects section, and
resume. Example wording:

> **Bengaluru Renter's Copilot** — Live dashboard flagging underpriced
> Bengaluru rental listings using an XGBoost pricing model (CV MAPE 22.5%).
> [Demo](https://huggingface.co/spaces/<you>/bengaluru-renter-copilot) ·
> [Code](https://github.com/rajveer-2004/bengaluru-renter-copilot)

## Troubleshooting

**"Application error"** on the Space right after push
→ Click the Space's "Logs" tab. Usually: missing dep in requirements.txt,
or a hardcoded local path in app.py.

**Blank dashboard, "No scored listings yet"**
→ You forgot to copy `db/copilot.db`. HF's filesystem is what you push, not
what's on your laptop. Re-check the copy step.

**"Rate limit exceeded" on git push**
→ HF caps large-file pushes on free tier. Our DB is ~500KB so this won't
hit; but if you ever add larger artifacts (models, embeddings), use
`git lfs`.
