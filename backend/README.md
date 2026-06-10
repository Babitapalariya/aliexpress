# AliExpress to Shopify FastAPI Middleware

FastAPI middleware for:

```text
AliExpress -> FastAPI Middleware -> Shopify
```

## Setup

```powershell
cd D:\project\python\aliexpress
python -m venv venv
.\venv\Scripts\python -m pip install -r requirements.txt
```

Update `.env` with your AliExpress, Shopify, and MySQL credentials.

Create the database:

```sql
CREATE DATABASE aliexpress_shopify;
```

Create tables:

```powershell
.\venv\Scripts\python -m app.create_tables
```

Run the API:

```powershell
deploy\restart.ps1
```

Stop the API:

```powershell
deploy\stop.ps1
```

Open AliExpress OAuth:

```text
http://localhost:8000/login
```

Useful endpoints:

```text
GET  /
GET  /login
GET  /callback?code=...
POST /refresh-token
GET  /product/{product_id}
POST /import/{product_id}
```

## AliExpress SDK

Place the official AliExpress `iop` SDK files in the `iop/` directory. The app imports it as `import iop`.


## Start backend
uvicorn app.main:app --reload --port 8001
python -m uvicorn app.main:app --reload --port 8001

## Backend URL
http://127.0.0.1:8001/docs


## Start frontend
node server.js

## Frontend URL
http://localhost:3001/
