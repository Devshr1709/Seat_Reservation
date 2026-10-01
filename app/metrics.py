from prometheus_client import Counter, Gauge

CONFIRMED = Counter("reservations_confirmed_total", "Reservations confirmed")
SEATS_CONFIRMED = Counter("seats_confirmed_total", "Seats confirmed via reservations")
DECLINED = Counter("reservations_declined_total", "Reservations declined", ["reason"])
CANCELLED = Counter("reservations_cancelled_total", "Reservations cancelled")
SEATS_AVAILABLE = Gauge("seats_available", "Available seats", ["show_id"])
SEATS_CONFIRMED_G = Gauge("show_seats_confirmed", "Confirmed seats (from DB)", ["show_id"])
HTTP = Counter("http_requests_total", "HTTP requests", ["method", "status"])
