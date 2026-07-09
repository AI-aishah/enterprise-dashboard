# Enterprise Overview Dashboard

The dashboard is served by a dependency-free Python service. It has one page per workbook sheet (Departments, Employees, Projects, Tasks, Meetings, Weekly Updates, Activity Log, Lists), each with a live chart overview and a searchable, sortable data table, all rendered from JSON pulled straight out of the database - nothing is hardcoded. Its Gemini assistant answers questions by generating read-only SQL against the same tables, rebuilt from `sample_data.xlsx`; workbook records are not copied into prompts wholesale.

## Run

```bash
cd chatbot
python3 init_db.py
python3 server.py
```

Open <http://127.0.0.1:8000>. Configure `GEMINI_API_KEY` and optionally `GEMINI_MODEL`, `DASHBOARD_HOST`, and `DASHBOARD_PORT` in `chatbot/.env` or the process environment.

For OTP email delivery through Gmail, add:

```bash
GMAIL_ADDRESS=youraddress@gmail.com
GMAIL_APP_PASSWORD=abcdefghijklmnop
```

Generate the Gmail app password from Google Account -> Security -> 2-Step Verification -> App Passwords -> Mail, then paste the 16-character code without spaces.

When previewing `dashboard_pages` with VS Code Live Server, keep `python3 server.py`
running on port 8000. Live Server serves only the HTML, CSS, and JavaScript; the
frontend automatically requests workbook data and chat responses from the Python
server at `http://127.0.0.1:8000`.

If the static HTML is hosted separately, set the `dashboard-api` meta tag in each `dashboard_pages/*.html` file to the full `/ask` endpoint URL.

## Run With Docker

From the project root:

```bash
docker compose up --build
```

Open <http://127.0.0.1:8000>.

The container reads environment variables from `chatbot/.env` and binds the server
to `0.0.0.0` so Docker port publishing works. Runtime data is stored in the
`dashboard-data` Docker volume:

- `/data/sample_data.xlsx`
- `/data/auth_data.db`
- `/data/chatbot_data.db`

On the first start, Docker seeds the volume from the workbook and auth database in
this project. After that, edits made through the Admin page persist in the Docker
volume. To reset Docker data, remove the volume:

```bash
docker compose down -v
```

The Docker image sets these paths automatically:

```bash
DASHBOARD_DATA_DIR=/data
DASHBOARD_DB_PATH=/data/chatbot_data.db
DASHBOARD_AUTH_DB_PATH=/data/auth_data.db
```

## Pages

| Page | File |
| --- | --- |
| Overview | `dashboard_pages/index.html` |
| Departments | `dashboard_pages/departments.html` |
| Employees | `dashboard_pages/employees.html` |
| Projects | `dashboard_pages/projects.html` |
| Tasks | `dashboard_pages/tasks.html` |
| Meetings | `dashboard_pages/meetings.html` |
| Weekly Updates | `dashboard_pages/weekly-updates.html` |
| Activity Log | `dashboard_pages/activity-log.html` |
| Lists | `dashboard_pages/lists.html` |

Shared logic lives in `dashboard_pages/assets/styles.css` and `dashboard_pages/assets/app.js` (chart renderers, the search/sort/paginate table component, and the chat widget).

## Data API

Every page pulls its data from two JSON routes on the same server:

- `GET /api/tables` - metadata for every sheet: table name, row count, and column labels/types.
- `GET /api/data/<table_name>` - full rows for one sheet (e.g. `/api/data/data_projects`), whitelisted against the tables that actually exist in `chatbot_data.db`.

The running server watches the workbook metadata on every API request. When
`sample_data.xlsx` changes, it atomically rebuilds `chatbot_data.db`; open
dashboard pages check freshness every 20 seconds and reload after a successful
sync. You can still run `python3 init_db.py` manually when the server is stopped.

## Test

```bash
cd chatbot
python3 -m unittest -v
```
