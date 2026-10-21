# BookMyVenue

A deployable venue discovery and booking platform. Guests can search listings, pay for a time slot, follow booking approval, and manage cancellations. Venue owners can upload venue photos, manage requests, and view booking totals. The React frontend is served by Flask in production; PostgreSQL stores production data, with SQLite available for local development.

To View:https://book-my-venue-gamma.vercel.app/

## Run locally

### Quick start (Python + Node)

Requirements: Python 3.10+ and Node.js 20+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
```

In terminal 1 (enable demo listings and the demo owner for local development):

```powershell
$env:SEED_DEMO="true"
python backend/app.py
```

In terminal 2:

```powershell
npm install
npm run dev
```

Open http://localhost:5173. The Vite dev server forwards `/api` requests to Flask at port 5000. SQLite creates `backend/instance/bookmyvenue.db` automatically and seeds six Kerala sample venues and a demo owner account on first run.

Local demo owner: `owner@bookmyvenue.in` / `Venue123!`. Demo accounts and listings are only seeded when `SEED_DEMO=true`; Render defaults to a clean database, so public demo credentials are never created.

Create a guest account in the app to make bookings. Use the owner account to view the owner studio. Demo venue images use Unsplash URLs; replace these with venue-owner supplied images before a commercial launch.

### Run the complete app with Docker

Requirements: Docker Desktop.

```powershell
docker compose up --build
```

Open http://localhost:5000. Docker Compose starts the React build, Flask API, and PostgreSQL with a persistent database volume. Set `POSTGRES_PASSWORD` and `JWT_SECRET_KEY` in a local `.env` file before exposing the service beyond your machine.

## Deploy

The repository includes `render.yaml` and a production Dockerfile for Render. Push this repository to a Git provider, create a new Blueprint in Render from the repository, and apply the proposed web service and PostgreSQL database. Render supplies `DATABASE_URL` and creates a strong `JWT_SECRET_KEY`. After deployment, open the service URL and test `/api/health` and the UI. Keep the database attached to the service so venue and booking data persist across deploys.

For another Docker host, build and run the same image and supply:

- `DATABASE_URL`: PostgreSQL connection string (`postgresql://...` supported) or SQLite URL for a single-process demo.
- `JWT_SECRET_KEY`: a long random secret unique to the deployment.
- `PORT`: listening port (defaults to 5000).

HTTPS is supplied by the deployment platform or reverse proxy. The Render service is configured for Razorpay mode so it will not accept fake demo payments. Add the production provider credentials listed below before enabling live customer checkouts. Configure database backups before accepting real bookings.

## Provider setup for a live deployment

The app supports a complete local demo without third-party accounts. Demo checkout is clearly marked and never charges a card. For live operations, create the provider accounts and add their secrets to the Render service environment (or the hosting provider's secret manager). Never put provider secrets in React code or commit them to Git.

| Capability | Provider | Environment variables |
| --- | --- | --- |
| Card/UPI checkout and refunds | Razorpay | `PAYMENT_MODE=razorpay`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` |
| Durable venue image storage | Cloudinary | `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` |
| Booking emails | SMTP relay (for example, your domain email provider) | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL` |
| Booking SMS | Twilio | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` |

For Razorpay, enable Standard Checkout, add `https://<your-app-domain>/api/webhooks/razorpay` as a webhook URL, subscribe to `payment.captured` and `payment.failed`, and use the webhook signing secret from that webhook setup. Use Razorpay test keys first; change to live keys only after a test booking and refund succeed. Checkout orders hold a slot for 15 minutes. Captured payments are signature checked; owner declines and eligible customer cancellations request a full Razorpay refund. Configure your live Razorpay account and settlement settings before taking orders.

For local SMTP/SMS development, provider credentials can go in an untracked `.env` file copied from `.env.example`; Docker Compose forwards these values. Email and SMS are best effort: delivery errors are logged and do not undo a payment or booking. Customers and venue owners can provide an optional phone number at signup for Twilio texts.

Venue photos accept JPEG, PNG, or WebP files up to 8 MB. The backend verifies and normalizes them to WebP. Local development stores them under Flask's instance folder. Production requires Cloudinary credentials so uploaded photos survive container restarts and deploys.

Payments currently charge the BookMyVenue Razorpay account. Refunds are initiated automatically for eligible cancellations, but automatic venue-owner split payouts are not enabled; reconcile and pay venue owners through your configured settlement process. Set your commission and refund policy before inviting real customers.

## Features

- Search and filter active venues by city, category, and keyword.
- Venue details, amenities, price, capacity, and date-based time-slot availability.
- Guest and owner registration, hashed passwords, and signed access tokens.
- Conflict-safe bookings: the database enforces one active booking per venue and time slot, even when two customers submit together.
- Booking history and customer cancellation; venue owners can confirm or decline booking requests.
- Owner dashboard with listings, upcoming events, booking counts, and booking value.
- Responsive desktop and mobile layouts.

## API overview

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Health check |
| POST | `/api/auth/register` | Create guest or owner account |
| POST | `/api/auth/login` | Sign in |
| GET | `/api/venues` | Search and filter venue listings |
| GET | `/api/venues/:id/availability?date=YYYY-MM-DD` | Read available slots |
| POST | `/api/bookings` | Reserve a slot (signed-in guest) |
| POST | `/api/checkout/order` | Hold a slot and create a demo or Razorpay checkout |
| POST | `/api/checkout/verify` | Verify the signed Razorpay checkout result |
| POST | `/api/webhooks/razorpay` | Receive captured and failed payment events |
| GET | `/api/bookings/mine` | Guest booking history |
| GET | `/api/owner/dashboard` | Owner listings, bookings, and stats |
| POST | `/api/owner/venues` | Publish an owner venue |
| PATCH | `/api/owner/bookings/:id` | Confirm or decline a booking |
| POST | `/api/owner/uploads` | Securely upload a venue image |

Bookings use three-hour slots starting at 09:00, 12:00, 15:00, 18:00, and 21:00 India time. This is an operational MVP: before broad commercial launch, add venue verification, a written cancellation policy, support tooling, and automated venue-owner payouts.
