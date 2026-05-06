# Dummy Tender API

Simple zero-dependency Node.js backend for tender dummy data and PDF files.

## Run

```bash
npm start
```

Server default port: `3000`

Use a custom port:

```bash
$env:PORT=4000; npm start
```

## Endpoints

- `GET /` - API health and endpoint list
- `GET /api/tenders` - tender list with dummy tender fields and PDF URLs
- `GET /api/tenders/:id` - single tender by ID
- `GET /api/tenders/:id/pdf` - view tender PDF
- `GET /api/tenders/:id/pdf?download=true` - download tender PDF
- `GET /pdf/:filename` - direct PDF access from the `PDF` folder

Example:

```text
http://localhost:3000/api/tenders
http://localhost:3000/api/tenders/1
http://localhost:3000/api/tenders/1/pdf
```
