# Six-Country Info Insights (六国用研洞察)

Automated daily intelligence digest covering **Russia, India, Indonesia, Nigeria, Kenya, and Pakistan** — designed to fuel user-research and product-insight workflows.

Collects 40+ RSS sources across six dimensions, uses **Google Gemini AI** to summarise, translate to Chinese, filter irrelevant/unsafe content, and delivers a structured digest via **Feishu bot** and/or **email**.

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
| `FEISHU_BOT_CHAT_ID` | Target Feishu group chat ID(s) |

Optional:
| Variable | Description |
|----------|-------------|
| `SMTP_USER` / `SMTP_PASSWORD` | Gmail SMTP credentials |
| `TO_EMAIL` | Email recipient |
| `FEISHU_ADMIN_OPEN_ID` | Your Feishu Open ID for document admin access |
| `FEISHU_FOLDER_TOKEN` | Feishu folder to store generated PDFs |

### 5. Run

```bash
python main.py
```

## GitHub Actions

The workflow (`.github/workflows/daily-digest.yml`) runs automatically every day at **07:00 Beijing time**.

Add these **Repository Secrets** in GitHub:
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_SA_JSON`
- `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_BOT_CHAT_ID`
- `FEISHU_ADMIN_OPEN_ID` (optional)
- `SMTP_USER`, `SMTP_PASSWORD`, `TO_EMAIL` (optional, for email)

## Project Structure

```
config/sources.yaml          # RSS source configuration (6 dimensions)
collectors/
  base.py                    # NewsItem dataclass + BaseCollector
  rss_collector.py           # RSS feed collector
processors/
  summarizer.py              # Gemini AI summarise + translate + filter
  deduper.py                 # Deduplication, date filtering, grouping
publishers/
  feishu_publisher.py        # Feishu doc + bot card publisher
templates/
  email.html                 # Jinja2 email/PDF template
email_sender.py              # SMTP email sender + PDF generation
main.py                      # Entry point
.github/workflows/           # CI/CD
```

## Customisation

- **Add/remove RSS sources**: edit `config/sources.yaml`
- **Adjust categories**: update `category_order` and `category_names` in the `output` section
- **Tune AI behaviour**: modify prompts in `processors/summarizer.py`

## License

MIT
