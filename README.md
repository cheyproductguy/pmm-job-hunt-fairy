# 🎯 AI-Powered PMM Job Radar (Bilingual EN/FR)

An automated, content-first job search pipeline built with Python and GitHub Actions. Designed specifically for Product Marketing Managers (PMM) and Go-to-Market (GTM) professionals seeking roles in France, Geneva, and remote Europe. 

Rather than relying purely on brittle job titles, this engine scans job descriptions for core competencies (*positioning, sales enablement, GTM, value proposition*) and leverages the OpenAI API to act as an executive career coach.

---

## 🚀 Key Features

* **Content-First Matching:** Evaluates job descriptions for core PMM DNA rather than filtering out non-standard titles (catching roles like *Growth Manager* or *Product Lead* that require PMM skills).
* **B2C & B2B2C Scoring Boost:** Automatically floats consumer-facing tech, direct-to-consumer, and B2B2C roles to the top of your digest.
* **AI Executive Coach (OpenAI `gpt-4o-mini`):** Reads your extended bilingual profile (`cv.txt`) against live job postings to generate a punchy 2-sentence rationale and gap analysis (highlighting location or industry gaps).
* **Bilingual Support (EN/FR):** Aggregates international and local postings across France and Switzerland in both English and French.
* **Smart Deduplication:** Maintains a local `.job_history.json` record to ensure you never receive duplicate alerts for jobs you've already seen.
* **Clean HTML Email Digests:** Delivers beautifully formatted mobile-friendly job cards directly to your inbox every morning via GitHub Actions.

---

## 🛠️ Tech Stack & Infrastructure

* **Automation:** GitHub Actions (scheduled cron job + manual dispatch)
* **Job Aggregator:** Adzuna API (aggregating LinkedIn, Indeed, APEC, Welcome to the Jungle, France Travail, etc.)
* **Intelligence:** OpenAI API (`gpt-4o-mini`)
* **Core Language:** Python 3.x (`requests`, `PyYAML`, `openai`)
* **Notifications:** Secure SMTP (Gmail App Passwords + HTML Email Templates)

---

## ⚙️ Configuration (`config.yaml`)

The pipeline's scoring weights and targeting criteria are fully customizable inside `config.yaml`:
* **Core Competencies:** Positioning, sales enablement, GTM, product messaging, product launch, value proposition.
* **Business Model Focus:** B2C, B2B2C, consumer, direct-to-consumer.
* **Geographic Scope:** Local hubs (Chamonix, Annecy, Geneva, Haute-Savoie) + nationwide France and Europe-wide remote.
* **Negative Filters:** Automatically skips senior leadership (Director, VP, CMO), internships, engineering, and hardware roles.

---

## 🔐 Required GitHub Secrets

To run this pipeline successfully in GitHub Actions, configure the following secrets under your repository **Settings > Secrets and variables > Actions**:

| Secret Name | Description |
| :--- | :--- |
| `ADZUNA_APP_ID` | Your Adzuna API Application ID |
| `ADZUNA_APP_KEY` | Your Adzuna API Application Key |
| `OPENAI_API_KEY` | Your OpenAI API Key (`sk-...`) for AI rationales |
| `GMAIL_ADDRESS` | Your Gmail address used to send alerts |
| `GMAIL_APP_PASSWORD` | Your 16-character Gmail App Password |
| `JOB_ALERT_EMAIL` | The recipient email address where digests arrive |

---

## 🏃‍♂️ Manual Trigger

1. Navigate to your GitHub repository.
2. Click on the **Actions** tab.
3. Select **Daily PMM Job Hunter** on the left sidebar.
4. Click **Run workflow** > **Run workflow** to trigger an immediate scan and email dispatch.
