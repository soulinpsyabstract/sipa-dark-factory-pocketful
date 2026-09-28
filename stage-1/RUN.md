# Pocketful Stage 1 - Run Instructions

## Prerequisites

```bash
pip install bcrypt
```

## Run Locally

```bash
python app.py
```

Server runs on port 8080 by default. Set `PORT` env var to customize:

```bash
PORT=3000 python app.py
```

## Docker

### Build

```bash
docker build -t pocketful-stage-1 .
```

### Run

```bash
docker run -p 8080:8080 pocketful-stage-1
```

### With custom port

```bash
PORT=3000 docker run -p 3000:3000 pocketful-stage-1
```

## Testing

```bash
python -m harness run --track pocketful --repo ./pocketful --stage 1
```

Note: The test harness path may need adjustment based on your test setup.
