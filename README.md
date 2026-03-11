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