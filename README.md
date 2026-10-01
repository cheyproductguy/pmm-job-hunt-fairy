# PMM Job Hunter

## About

PMM Job Hunter is a daily job-finding assistant for Cheyenne's search across product marketing and adjacent roles. It looks for work in France and Geneva that uses strengths in go-to-market strategy, customer research, positioning, growth, energy efficiency, insurance, and regional marketing—even when the job title does not say “Product Marketing Manager.”

Each new listing is scored against a configurable experience profile and emailed as a sorted digest. The active `adjacent_discovery` profile gives transferable responsibilities and role scope more weight, making it easier to spot relevant opportunities under unfamiliar titles. Remote roles are included but rank below suitable Chamonix-area and Geneva hybrid or office roles.

When `OPENAI_API_KEY` is configured, the digest can also include a short, evidence-based fit rationale for up to ten top-scoring roles. This is optional; searching and email delivery do not depend on it.

GitHub Actions can run the search daily at 7:00 AM Paris time. The application uses Adzuna for job listings and Gmail SMTP for the digest.

## Setup

1. Create an Adzuna API application and note its app ID and key.
2. Create a Gmail App Password for the account that will send alerts. Keep it private.
3. Add these repository secrets in **Settings → Secrets and variables → Actions**:
   - `ADZUNA_APP_ID`
   - `ADZUNA_APP_KEY`
   - `GMAIL_ADDRESS` (the sending Gmail address)
   - `GMAIL_APP_PASSWORD`
   - `JOB_ALERT_EMAIL` (optional; defaults to `notification_email` in `config.yaml`)
   - `OPENAI_API_KEY` (optional; enables short role-fit rationales)
4. Enable GitHub Actions and ensure this workflow is on the repository's default branch.
5. Run **Daily PMM Job Hunter** manually once from the Actions tab to confirm the secrets and APIs are configured.

Install and run locally with Python 3.10+:

```bash
python -m pip install -r requirements.txt
```

Set the five environment variables above, then run `python main.py`. Adzuna credentials are required for searching. Gmail credentials are only required when there are fresh matches to email. The local `.job_history.json` file tracks postings already emailed; GitHub Actions preserves it between runs with its cache.

Edit `config.yaml` to adjust titles, locations, thresholds, and scoring. `cv.txt` is the matching profile and can be tailored as the CV changes.

## Scoring and filters

The app searches configured English and French queries plus title variants, so roles such as product strategy, customer insights, growth, proposition, offer, and regional marketing can surface even when they are not called PMM. Competency matching includes region-specific market strategy, energy efficiency and demand reduction programs, paid media agency coordination, project management, creative briefs/direction, and campaign reporting. It checks negative keywords against the **job title only**, avoiding false exclusions when descriptions mention sales or engineering partners.

Competencies, industries, business models, location/workplace, language signals, and sponsorship evidence are scored as separate dimensions. English and French phrase variants are accent-insensitive. English-first roles rank well, French postings remain eligible, and unspecified language requirements receive a neutral score. Geneva visa sponsorship evidence improves fit; it is not a hard filter unless `visa_sponsorship.required` is changed to `true`.

Fit scores are normalized to 10. `scoring.active_profile` selects `balanced`, `cv_evidence`, or `adjacent_discovery`; adjust `search.minimum_fit_score` to control how selective the email is. Chamonix-area and Geneva hybrid/in-office roles rank above remote roles.

To switch a daily run, set `scoring.active_profile` in `config.yaml`, then commit and push that change to the repository's default branch. The next scheduled run reads that profile. `balanced` gives a mix of demonstrated skills, domain, and location; `cv_evidence` emphasizes the competency groups you have directly used; `adjacent_discovery` emphasizes transferable responsibilities and role scope so titles can vary more.

## Notes

- Adzuna results are subject to API availability and listing coverage in each country.
- Temporary Adzuna errors (including HTTP 503) are retried with backoff. If a few individual pages remain unavailable, the app continues with partial results; if no pages succeed, the run exits with a clear retry-later error.
- Job boards may omit visa and language details. Unspecified details receive a neutral score; Swiss sponsorship is not required by default.
- GitHub scheduled runs use the latest commit on the default branch. GitHub may occasionally delay scheduled runs during heavy load.
