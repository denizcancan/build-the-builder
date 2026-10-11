# freya-statement-mock

Cloudflare Worker that mocks Freya Bank's seven statement/debt services (service contract 2026-10-01, sections 3-6). For the test customers, it returns exactly the bodies and HTTP status codes from `test-customers.json`. It does not keep state.

## Files

- `worker.js`: routing, the lookups and the contract's error responses.
- `data.js`: GENERATED from `test-customers.json` by `build_data.py`. Do not edit it.
- `build_data.py`: regenerates `data.js`. Run `python3 -I build_data.py [test-customers.json] [data.js]`.
- `test_mock.py`: compares every entry in the test file, plus error cases, against a running mock. Run `python3 -I test_mock.py [base_url]`.

## Run locally

```bash
python3 -I build_data.py      # only needed if test-customers.json changed
npx wrangler dev              # http://localhost:8787
python3 -I test_mock.py       # expects 617 of 617 matched
```

## Deploy

```bash
npx wrangler login
npx wrangler deploy           # https://freya-statement-mock.<subdomain>.workers.dev
python3 -I test_mock.py https://freya-statement-mock.<subdomain>.workers.dev
```

## Endpoints

- `GET /health` returns `{"status":"ok"}`.
- `POST /oauth2/token` returns a dummy token. Tokens are never checked.
- `POST /voice/v1/cards.list`, `cards.balance`, `statement.week.get`, `statement.week.decide`, `statement.week.change`, `contact.email.get` and `statement.pdf.send`. Each takes a body of the form `{"data": {...}, "call": {...}}`. The same services also answer without the `/voice/v1` prefix.

When a `data` value is an unfilled `{{...}}` template, the mock answers for customer 90000001 and card `540000******1234`.
