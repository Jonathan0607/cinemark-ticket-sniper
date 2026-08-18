# Cinemark ticket sniper

Discord alerts when seats open up at a sold-out Cinemark showing.

Point it at any Cinemark theater and movie, say which rows and showtimes you
would accept, and it notifies your Discord channel when a matching seat frees up. Runs on GitHub Actions or locally.

Built to catch cancellations for The Odyssey in IMAX 70mm, which sold out
weeks ahead at every theater that can project it. Good seats reappear all the
time. Someone returns two tickets, a hold expires, and the seats go to whoever
happens to be looking. This looks every 30 minutes so you don't have to.

## Setup

1. Fork this repo. Keep the fork public: public repos get unlimited free
   Actions minutes.
2. Edit `config.toml` with your theater, movie, and seat preferences (below).
3. Set your Discord Webhook URL as a repository secret named `DISCORD_WEBHOOK` (or as an environment variable `DISCORD_WEBHOOK` locally).
4. Delete `state.json` and `alerts.log`, they belong to this repo's hunt.
5. Enable workflows on your fork (Actions tab, one button).
6. Run the `watch` workflow once by hand (Actions, then watch, then Run
   workflow). The first sweep records a quiet baseline and alerts start with
   the second.

## Config

Everything lives in `config.toml`:

| key | meaning |
|---|---|
| `theater` | slug from the theater page URL: `cinemark.com/theatres/<slug>` |
| `movie_id` | numeric id for the movie (finding it: below) |
| `movie_name` | only used in alert text |
| `timezone` | the theater's IANA timezone, e.g. `America/Chicago` |
| `excluded_rows` | rows you refuse, e.g. `["A", "B", "C", "D"]` |
| `earliest_showtime` / `latest_showtime` | accept window, 24h `HH:MM`, theater-local |
| `party_size` | alert only when this many adjacent seats open together |

To find `movie_id`: open your theater's page on cinemark.com, right-click any
showtime of your movie, and copy the link. It looks like
`/TicketSeatMap/?TheaterId=...&CinemarkMovieId=104867&...` and that number
is it.

## How it works

Cinemark's site is server-rendered, so dates, showtimes, and seat maps are all
plain HTML. On each run, an Actions job fetches the seat map of every showing
that passes your filters, diffs availability against the previous run (state
is a JSON snapshot the job commits back to the repo), and on any newly opened
seat it sends a POST request to your Discord webhook. The job paces itself to about six requests a minute
because Cinemark rate-limits around 60-70 requests per ten minutes, so a full
sweep takes about 20 unhurried minutes.
