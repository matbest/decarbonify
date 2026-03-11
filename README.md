# decarbonify

Portfolio Carbon Insight Tool (Streamlit)

This app lets an organisation describe its property portfolio as a hierarchical set of assets (land → buildings → rooms → equipment, etc.), then:

- Browse the asset hierarchy
- View details for a selected asset
- Get AI-generated recommendations to reduce emissions or increase sequestration
- Chat with an assistant about portfolio-level priorities

## Run locally

1. Create a virtual environment and install dependencies:

	 - `pip install -r requirements.txt`

2. Start Streamlit:

	 - `streamlit run streamlit_app.py`

The app loads `portfolio.json` by default.

## Login (Google)

This app uses **Google OAuth (OpenID Connect)** for login.

Do not commit the downloaded Google OAuth JSON file (it contains a `client_secret`). Copy the values into Streamlit Secrets instead.

### 1) Create Google OAuth credentials

In Google Cloud Console:

1. Create/select a project.
2. Configure the OAuth consent screen.
3. Create an **OAuth Client ID** (type: Web application).
4. Add **Authorized redirect URIs**:

   - Local: `http://localhost:8501/`
   - Streamlit Community Cloud: `https://YOUR-APP-NAME.streamlit.app/`

### 2) Add Streamlit Secrets

- Streamlit Community Cloud: paste secrets in the app's **Secrets** UI (no file needed).
- Local dev (option A): copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`.
- Local dev (option B): set environment variables (see below).

Secrets format:

```toml
[google]
client_id = "..."
client_secret = "..."
redirect_uri = "http://localhost:8501/"  # or your Streamlit Cloud URL

# Optional restrictions (recommended)
# allowed_domains = ["yourcompany.com"]
# allowed_emails = ["alice@yourcompany.com"]
```

If you set `allowed_domains` or `allowed_emails`, only those accounts can sign in.

### Local dev without a secrets file

You can run locally without `.streamlit/secrets.toml` by setting environment variables:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REDIRECT_URI` (e.g. `http://localhost:8501/`)
- Optional: `GOOGLE_ALLOWED_DOMAINS` (comma-separated)
- Optional: `GOOGLE_ALLOWED_EMAILS` (comma-separated)

## Optional: enable AI recommendations

Set an OpenAI API key before running:

- Windows PowerShell: `setx OPENAI_API_KEY "your-key"`

Optionally set a model:

- `setx OPENAI_MODEL "gpt-4o-mini"`

If `OPENAI_API_KEY` is not set, the app uses a lightweight heuristic fallback.

## Portfolio JSON format

A portfolio is stored as JSON with a name and a list of assets. Each asset can include nested `assets`.

Example:

```json
{
	"portfolio_name": "Bradwell Parish",
	"assets": [
		{
			"name": "Heelands Site",
			"type": "land",
			"assets": [
				{
					"name": "Heelands Meeting Centre",
					"type": "building",
					"assets": [
						{"name": "Gas Boiler", "type": "energy_system", "fuel": "gas"}
					]
				}
			]
		}
	]
}
```

## Deploy (Streamlit Community Cloud)

1. Push this repo to GitHub.
2. In Streamlit Community Cloud, create an app pointing at this repository.
3. Set the main file to `streamlit_app.py`.
4. Add secrets (optional) in Streamlit Cloud settings:

	 - `OPENAI_API_KEY`
	 - `OPENAI_MODEL`

On every `git push`, Streamlit Cloud rebuilds and updates the app.