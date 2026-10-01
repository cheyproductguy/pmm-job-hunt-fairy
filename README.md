# PMM Job Hunter

A small Python job finder that searches Adzuna in France and Switzerland, scores Product Marketing Manager roles against `cv.txt`, and emails new matches through Gmail. GitHub Actions can run it daily at 7:00 AM Paris time.

## Setup

1. Create an Adzuna API application and note its app ID and key.
2. Create a Gmail App Password for the account that will send alerts. Keep it private.
3. Add these repository secrets in **Settings → Secrets and variables → Actions**:
   - `ADZUNA_APP_ID`
   - `ADZUNA_APP_KEY`
   - `GMAIL_ADDRESS` (the sending Gmail address)
   - `GMAIL_APP_PASSWORD`
   - `JOB_ALERT_EMAIL` (defaults in `config.yaml` to the address on the CV)
4. Enable GitHub Actions and ensure this workflow is on the repository's default branch.
5. Run **Daily PMM Job Hunter** manually once from the Actions tab to confirm the secrets and APIs are configured.

Install and run locally with Python 3.10+:

```bash
python -m pip install -r requirements.txt
```

Set the five environment variables above, then run `python main.py`. Adzuna credentials are required for searching. Gmail credentials are only required when there are fresh matches to email. The local `.job_history.json` file tracks postings already emailed; GitHub Actions preserves it between runs with its cache.

Edit `config.yaml` to adjust titles, locations, thresholds, and scoring. `cv.txt` is the matching profile and can be tailored as the CV changes.

## Scoring and filters

Roles are searched with English and French title variants. Listings with configured negative keywords are removed. Roles with an English-language requirement or English-first signal are retained; French-language ads are searchable, and the profile's professional French is included in the CV text. Matching uses English/French pairs for PMM skills, gives an industry boost, and prioritizes hybrid or office work in the Chamonix/Geneva corridor over remote roles. Swiss listings must mention a configured sponsorship signal.

Fit scores are capped at 10. Only new listings at or above `minimum_fit_score` are included in the HTML email.

## Notes

- Adzuna results are subject to API availability and listing coverage in each country.
- Job boards may omit visa and language details. Sponsorship and English-signal filtering can therefore exclude relevant listings whose ads leave those details unstated.
- GitHub scheduled runs use the latest commit on the default branch. GitHub may occasionally delay scheduled runs during heavy load.
