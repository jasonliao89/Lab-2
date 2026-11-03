# Lab-2

SEC EDGAR collection CLI for the data steps of an equity-research workflow.

Create a `.env` file locally (it is ignored by Git) with a descriptive SEC identity:

```text
SEC_USER_AGENT=Your Name contact@example.com
API_KEY=your_openai_api_key
```

Install:

```powershell
python -m pip install -r requirements.txt
```

Run all seven steps, with peers and a price for P/E:

```powershell
python sec_research.py TICKER --peers PEER1 PEER2 --price 123.45
```

Run one numbered step only:

```powershell
python sec_research.py TICKER --step 4
```

Outputs are saved under `research_output/<SEC issuer name>_<run timestamp>/`. The CLI reads the User-Agent only from `SEC_USER_AGENT` and puts it on every request to SEC endpoints.

The default run is Lab 02 only: seven SEC/Python data steps with no OpenAI call and no `API_KEY` requirement. Add `--report` to opt into the later model-role workflow; it then requires `API_KEY` and saves `equity_research_report.md` beside the source texts and facts. Set `OPENAI_MODEL` in `.env` to override the default `gpt-4.1`.
