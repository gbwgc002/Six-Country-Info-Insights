# Seven-Country Info Insights (七国用研洞察)

Automated daily intelligence digest covering **EE1, India, Indonesia, Nigeria, Kenya, Pakistan, and Bangladesh** — designed to fuel user-research and product-insight workflows.

Collects 70+ enabled RSS sources across six dimensions, uses **Google Gemini AI** to summarise, translate, filter irrelevant/unsafe content, and delivers a structured digest via **Feishu bot** and/or **email**.

The repository also contains an independent **AI Insights Weekly** pipeline
for user-research and consumer-insight teams. It does not change the
seven-country source list, processing prompt, delivery format, or schedule.

## Coverage Dimensions

| Dimension | Description | Example Sources |
|-----------|-------------|-----------------|
| 🏛️ Macro & Infrastructure | Government policies, 5G rollout, power grid, disasters | BBC (7 languages), Al Jazeera, Light Reading, Mobile World Live |
| 💰 Commerce & Economy | Inflation, e-commerce, fintech, mobile money | Rest of World, KrASIA, Inc42, Economic Times, BusinessDay NG |
| 🚀 Digital Ecosystem | Startup funding, app trends, local tech | TechCabal, TechPoint Africa, Disrupt Africa, Techweez |
| 🎭 Pop Culture & Sentiment | Gen Z trends, festivals, memes, social media | Global Voices, Vice, Mashable, AllAfrica, Daily Trust |
| 📱 Mobile Market | Smartphone launches, brand dynamics | GSMArena, Gadgets 360, FoneArena, PhoneRadar |
| 🌍 Country Headlines | General breaking news per country | Moscow Times, TASS, Times of India, Punch, The Standard, Geo News |

## Setup

### 1. Prerequisites

- Python 3.11+
- A Google Cloud project with Vertex AI enabled and a service account JSON key
- (Optional) Gmail account with app password for email delivery
- (Optional) Feishu self-built app for bot delivery

### 2. System Dependencies

**macOS:**
```bash
brew install pango libffi cairo
```

**Ubuntu / Debian:**
```bash
sudo apt-get install -y libcairo2 libpango-1.0-0 libpangocairo-1.0-0 \
    libgdk-pixbuf2.0-0 libffi-dev shared-mime-info \
    fonts-noto-cjk fonts-wqy-zenhei fonts-wqy-microhei
```

### 3. Install Python Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Required variables:
| Variable | Description |
|----------|-------------|
| `GOOGLE_CLOUD_PROJECT` | Your GCP project ID |
| `GOOGLE_SA_JSON` or `GOOGLE_SA_FILE` | Service account credentials |
| `FEISHU_APP_ID` | Feishu app ID |
| `FEISHU_APP_SECRET` | Feishu app secret |
| `FEISHU_BOT_CHAT_ID` | Local-run target Feishu group chat ID(s) |

Optional:
| Variable | Description |
|----------|-------------|
| `SMTP_USER` / `SMTP_PASSWORD` | Gmail SMTP credentials |
| `TO_EMAIL` | Email recipient |
| `FEISHU_ADMIN_OPEN_ID` | Your Feishu Open ID for document admin access |
| `FEISHU_ARCHIVE_ROOT_FOLDER_TOKEN` | Shared Feishu root folder containing the two report folders |
| `FEISHU_SIX_COUNTRY_FOLDER_NAME` | Existing combined-report archive folder name; retained for backward compatibility |
| `FEISHU_AI_INSIGHTS_FOLDER_NAME` | AI report folder name; defaults to `AI洞察报告` |
| `FEISHU_COUNTRY_FOLDER_NAME_<COUNTRY>` | Optional exact child-folder override for a country report; normally auto-resolved from the country name |
| `FEISHU_FOLDER_TOKEN` | Legacy fallback upload folder when archive routing is unavailable |

### 5. Run

```bash
python main.py
```

## GitHub Actions

The workflow (`.github/workflows/daily-digest.yml`) runs automatically every day at **07:00 Beijing time**.

The five country-specific reports are independent from the daily seven-country
digest. Their workflows run as follows:

| Workflow | UTC+8 schedule | Behaviour |
|---|---:|---|
| `country-insights-daily-collect.yml` | Daily 07:30 | Collect one shared India, Indonesia, Nigeria, Pakistan, and Bangladesh candidate pool; no PDF and no group message |
| `country-insights-weekly.yml` | Monday 07:00 | Finalize the previous Monday-Sunday period, generate five bilingual PDFs, archive them, and send each report to its country group |

The manual dispatch supports either the internal site-management test group or
the five production country groups. All requested countries share one collection
pass before their independent AI review and ranking. Each country PDF is uploaded
directly into the matching country child folder under the shared archive root,
then ownership is transferred to `FEISHU_ADMIN_OPEN_ID` while the bot retains
full access. Supported child-folder labels include the Chinese or English country
name, with optional `洞察报告` / `Weekly Insights` suffixes.

The scheduled weekly workflow remains disabled until the repository variable
`ENABLE_COUNTRY_WEEKLY_REPORTS` is set to `true`, so production delivery cannot
start before all five country-group secrets are configured.

The independent AI Insights workflows run as follows:

| Workflow | UTC+8 schedule | Behaviour |
|---|---:|---|
| `ai-insights-daily.yml` | Daily 07:30 | Collect, score, deduplicate, and append candidates to the current natural-week Feishu document; no group message |
| `ai-insights-weekly.yml` | Monday 16:47 | Finalize the previous natural week, generate and upload a styled PDF, then send one PDF-linked card to 软件用研 |
| `ai-design-combined-weekly.yml` | After the weekly AI workflow completes | Validate the latest design JSON feed, then send the combined design + user-research card to SW用户体验部; failures alert AI2D作业测试群 |

Both pipelines use `gemini-3.6-flash`. The model can still be overridden with
the `GEMINI_MODEL` environment variable for manual runs.

Add these **Repository Secrets** in GitHub:
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_SA_JSON`
- `FEISHU_APP_ID`, `FEISHU_APP_SECRET`
- `FEISHU_GROUP_RUANJIANYONGYAN_ID` (软件用研)
- `FEISHU_GROUP_SWYONGHUTIYANBU_ID` (SW用户体验部)
- `FEISHU_GROUP_AI2DZUOYECESHIQUN_ID` (AI2D作业测试群)
- `FEISHU_GROUP_ZHANDIANGUANLIYONGYANNEIBU` (country-report test group)
- `FEISHU_GROUP_INDIA_ID`, `FEISHU_GROUP_INDONESIA_ID`
- `FEISHU_GROUP_NIGERIA_ID`, `FEISHU_GROUP_PAKISTAN_ID`
- `FEISHU_GROUP_BANGLADESH_ID`
- `FEISHU_ADMIN_OPEN_ID` (optional)
- `SMTP_USER`, `SMTP_PASSWORD`, `TO_EMAIL` (optional, for email)

## Project Structure

```
config/sources.yaml          # RSS source configuration (7 countries, 6 dimensions)
config/ai_insights_sources.yaml # Independent AI Insights sources and thresholds
collectors/
  base.py                    # NewsItem dataclass + BaseCollector
  rss_collector.py           # RSS feed collector
processors/
  summarizer.py              # Gemini AI summarise + translate + filter
  deduper.py                 # Deduplication, date filtering, grouping
publishers/
  feishu_publisher.py        # Feishu doc + bot card publisher
templates/
  email.html                 # Shared Jinja2 email/PDF visual template
email_sender.py              # SMTP email sender + PDF generation
main.py                      # Seven-country daily entry point
country_candidate_store.py   # Persistent daily pool for five-country weekly reports
country_report.py            # Shared-pool bilingual country weekly report runner
ai_insights.py               # AI Insights collect/publish entry point
.github/workflows/           # CI/CD
```

## Feishu report archive

New seven-country PDFs keep using the existing report archive folder for
backward compatibility. New AI weekly
documents and PDFs are stored in `AI洞察报告`. When
`FEISHU_ADMIN_OPEN_ID` is configured and the app has the ownership-transfer
scope, ownership is transferred to that user while the bot retains
`full_access`.

Historical migration is isolated in
`.github/workflows/feishu-archive-migration.yml`. It is manual-only and
defaults to a read-only dry run. Select `execute=true` only after reviewing
the candidate list. The migration never sends a Feishu group message.

## Customisation

- **Add/remove RSS sources**: edit `config/sources.yaml`
- **Adjust AI Insights sources**: edit `config/ai_insights_sources.yaml`
- **Adjust categories**: update `category_order` and `category_names` in the `output` section
- **Tune AI behaviour**: modify prompts in `processors/summarizer.py`

## License

MIT
