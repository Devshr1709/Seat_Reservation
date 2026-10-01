BASE_URL ?= http://localhost:8000
ADMIN_TOKEN ?= admin-secret
up:
	docker compose up --build -d
burst:
	python burst.py $(BASE_URL) --admin-token $(ADMIN_TOKEN)
