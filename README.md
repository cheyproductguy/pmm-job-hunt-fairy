# PMM Job Hunter

## About

PMM Job Hunter is a daily job-finding assistant for Cheyenne's search across product marketing and adjacent roles. It looks for work in France and Geneva that uses strengths in go-to-market strategy, customer research, positioning, growth, energy efficiency, insurance, and regional marketing—even when the job title does not say “Product Marketing Manager.”

Eligible listings are scored against a configurable experience profile, and the top ten are emailed as a sorted digest each day. Listings can appear in more than one daily digest while they remain in Adzuna's results and meet the criteria. The active `adjacent_discovery` profile gives transferable responsibilities and role scope more weight, making it easier to spot relevant opportunities under unfamiliar titles. Remote roles are included but rank below suitable Chamonix-area and Geneva hybrid or office roles.

When `OPENAI_API_KEY` is configured, the digest can also include a short, evidence-based fit rationale for the top-scoring roles in the email. This is optional; searching and email delivery do not depend on it.

GitHub Actions runs the search daily at 7:15 AM Paris time. The 15-minute offset avoids the top-of-hour window when GitHub reports scheduled jobs are more likely to be delayed or dropped. The application uses Adzuna for job listings and Gmail SMTP for the digest.

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

Set the five environment variables above, then run `python main.py`. Adzuna credentials are required for searching. Gmail credentials are required when qualifying listings are found.

Edit `config.yaml` to adjust titles, locations, thresholds, and scoring. `cv.txt` is the matching profile and can be tailored as the CV changes.

## Scoring and filters

The app searches configured English and French queries plus title variants, so roles such as product strategy, customer insights, growth, proposition, offer, and regional marketing can surface even when they are not called PMM. Duplicate copies with the same normalized title, company, location, and country are collapsed even when Adzuna returns different listing IDs. Competency matching includes region-specific market strategy, energy efficiency and demand reduction programs, paid media agency coordination, project management, creative briefs/direction, and campaign reporting. It checks negative keywords against the **job title only**, avoiding false exclusions when descriptions mention sales or engineering partners.

Competencies, industries, business models, location/workplace, language signals, and sponsorship evidence are scored as separate dimensions. The digest shows a matched target industry when one is evident in the listing, with Adzuna's category as a fallback. English and French phrase variants are accent-insensitive. English-first roles rank well, French postings remain eligible, and unspecified language requirements receive a neutral score. Geneva visa sponsorship evidence improves fit; it is not a hard filter unless `visa_sponsorship.required` is changed to `true`.

Seniority screening excludes explicit 10+ year overall requirements and mandatory experience in a **pure B2B** role (with or without a numeric year count). Mixed B2B/B2C and B2B2C requirements remain eligible; preferred/desirable experience is not treated as mandatory. The daily digest is capped at the top ten matches (`search.max_email_results`), so optional AI rationales are generated only for those roles. The same listing may be emailed again on later days if it still appears in Adzuna and passes the filters. Listings older than `search.max_age_days` are excluded.

Fit scores are normalized to 10. `scoring.active_profile` selects `balanced`, `cv_evidence`, or `adjacent_discovery`; adjust `search.minimum_fit_score` to control how selective the email is. Chamonix-area and Geneva hybrid/in-office roles rank above remote roles.

To switch a daily run, set `scoring.active_profile` in `config.yaml`, then commit and push that change to the repository's default branch. The next scheduled run reads that profile. `balanced` gives a mix of demonstrated skills, domain, and location; `cv_evidence` emphasizes the competency groups you have directly used; `adjacent_discovery` emphasizes transferable responsibilities and role scope so titles can vary more.

## Notes

- Adzuna results are subject to API availability and listing coverage in each country.
- Temporary Adzuna errors (including HTTP 503) are retried with backoff. If a few individual pages remain unavailable, the app continues with partial results; if no pages succeed, the run exits with a clear retry-later error.
- Job boards may omit visa and language details. Unspecified details receive a neutral score; Swiss sponsorship is not required by default.
- GitHub scheduled runs use the latest commit on the default branch. GitHub may occasionally delay scheduled runs during heavy load.
