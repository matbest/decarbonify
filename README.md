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

## Optional: React Arborist sidebar (DnD + rename)

This repo includes an optional local Streamlit Component (React + react-arborist) that enables:

- Drag-and-drop reorder / re-parent
- Inline rename
- Single selection sync

If you don't build it, the app will fall back to `streamlit-arborist` (or a simple radio list).

Build the component frontend:

- `cd components/arborist_tree/frontend`
- `npm install`
- `npm run build`

Dev mode (hot reload):

- `cd components/arborist_tree/frontend`
- `npm install`
- `npm run dev`
- set `DECARBONIFY_ARB_DEV_URL` to the dev server URL (usually `http://localhost:5173`)

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

# Optional: persist edits to the user's Google Drive
# (creates a visible "Decarbonify" folder and saves one JSON per portfolio)
# drive_enabled = true
# drive_folder = "Decarbonify"

# Optional restrictions (recommended)
# allowed_domains = ["yourcompany.com"]
# allowed_emails = ["alice@yourcompany.com"]
```

If you set `allowed_domains` or `allowed_emails`, only those accounts can sign in.

### Google Drive persistence

If enabled, the app will request Drive access and store one JSON file per portfolio in a visible folder (default: `Decarbonify`) in the signed-in user's Google Drive.

Google Cloud requirements:

- Enable the **Google Drive API** for your project.
- Ensure the OAuth consent screen is configured to allow the Drive scope.

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

## Asset type templates (JSON + formulas)

This repo includes an in-repo library of reusable **asset type templates** in the `asset_types/` folder.

Each template is a JSON file that defines:
- **inputs**: fields the user should fill (stored in `asset.data_fields.<key>.manual.value`)
- **outputs**: derived fields computed from arithmetic-only formulas (stored in `asset.data_fields.<key>.derived.value`)

In the app, open an asset and use **Data → Asset type** to:
- Apply a template
- Fill inputs (manual overrides always win)
- Optionally ask AI to suggest missing inputs
- Compute outputs and write them back into the portfolio

Template schema (example):

```json
{
	"id": "gas_boiler",
	"label": "Gas boiler",
	"description": "...",
	"inputs": [
		{"key": "annual_gas_kwh", "label": "Annual gas use", "kind": "number", "unit": "kWh/year"},
		{"key": "gas_kgco2e_per_kwh", "label": "Gas carbon intensity", "kind": "number", "unit": "kgCO2e/kWh", "default": 0.184}
	],
	"outputs": [
		{"key": "emissions_tco2e_per_year", "kind": "number", "unit": "tCO2e/year", "formula": "(annual_gas_kwh * gas_kgco2e_per_kwh) / 1000"}
	]
}
```

Formula safety: formulas are evaluated using a restricted arithmetic-only parser (no function calls, no attribute access).

## Portfolio JSON format

A portfolio is stored as JSON with a name and a list of assets. Each asset can include nested `assets`.

Assets can also include a `data_fields` object for dynamic per-asset attributes. Each field stores two values:

- `derived.value`: produced by inference/lookups (e.g. LLM, heuristics)
- `manual.value`: entered by a human and takes precedence

The app expects (at minimum) an emissions field keyed as `emissions_tco2e_per_year`.

Example:

```json
{
	"portfolio_name": "Bradwell Parish",
	"assets": [
		{
			"name": "Heelands Site",
			"core_type": "place",
			"subtype": "site",
			"current_role": "passive",
			"location": "Heelands",
			"quantity": 1,
			"attributes": {},
			"assets": [
				{
					"name": "Heelands Meeting Centre",
					"core_type": "place",
					"subtype": "building",
					"current_role": "passive",
					"assets": [
						{
							"name": "Gas Boiler",
							"core_type": "energy_system",
							"subtype": "boiler",
							"current_role": "converter",
							"attributes": {"fuel": "gas"},
							"fuel": "gas",
							"data_fields": {
								"emissions_tco2e_per_year": {
									"label": "Emissions",
									"kind": "number",
									"unit": "tCO2e/year",
									"derived": {"value": 1.25, "source": "llm"},
									"manual": {"value": null}
								}
							}
						}
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