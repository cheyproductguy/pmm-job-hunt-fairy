# The PMM Job Hunt Fairy 🧚

Tired of trekking daily across **6+ job boards + 6+ job alerts**? Limited job board filters and mismatched listings can leave your next great PMM role hiding in plain sight.

**Send your PMM Job Hunt Fairy to find up to 10 roles matched to your PMM magic and career quest each morning.**

Spend less time searching and more time applying for your dream job—with your magical career-quest companion by your side.

Built for PMM, where great-fit roles often hide behind unexpected titles. Looking for a different kind of role? You can adapt the search by changing the titles and match criteria in `config.yaml`.

## How it works

- Searches Adzuna in English and French for PMM and adjacent roles with non-traditional titles.
- Scores roles against your skills, transferable experience, target industries, and work preferences.
- Gives recently posted roles a small nudge up the list.
- Emails up to 10 roles you haven’t received before. Keep the emails as your personal job-hunt archive.
- Optional quest note: why a role aligns with your experience and the main reason it may not.

## Make it yours

Give your PMM Job Hunt Fairy the details she needs to find your best matches. Your two career-quest control panels are:

- `config.yaml` — map your career quest: name, target titles, strengths, industries, locations, and search style.
- `cv.txt` — add your experience and skills so the matches reflect your background.

Pick a search style to match your career quest goals:

- **Matchmaker** (`balanced`) — a balanced mix of CV fit, industry, and location.
- **Proof Pixie** (`cv_evidence`) — prioritize roles backed by experience and skills in your CV.
- **Explorer** (`adjacent_discovery`, the default) — discover adjacent roles that use your transferable skills.

## Set up the daily email

Map your career quest once in `config.yaml`, and your PMM Job Hunt Fairy will set out to find your 10 best matches each morning. First, open the two portals she needs: Adzuna lets her scout job listings, and Gmail gives her a way to deliver your shortlist.

1. Create an Adzuna app ID and key, then create a Gmail App Password. These are the secure details that connect your fairy to the job listings and your inbox.
2. In your GitHub repository, open **Settings → Secrets and variables → Actions**. Add `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `GMAIL_ADDRESS`, and `GMAIL_APP_PASSWORD`.
3. Set the delivery address in `notification_email` in `config.yaml`, or use the `JOB_ALERT_EMAIL` secret to override it. You can also add `JOB_ALERT_NAME` for your greeting and `OPENAI_API_KEY` for the optional quest note.
4. Make sure GitHub Actions is enabled. You can click **Run workflow** once as a setup check; the daily schedule itself runs automatically.

Your shortlist arrives by email at **7:15 AM Paris time**. Want your fairy to make an extra scouting trip today? In your repository, open **Actions → Daily PMM Job Hunter → Run workflow**.
