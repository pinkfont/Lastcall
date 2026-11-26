# Deploying Lastcall (Oracle Cloud Always Free)

The server runs the API, scheduler and Telegram bot 24/7. Your **laptop** keeps doing
the one thing a server can't: signing in to POLITEMall to check your submissions. It
pushes the results to the server every 2 hours while it's on.

```
 Laptop (Windows)                          Server (Oracle, Ubuntu)
 ┌──────────────────────────┐   HTTPS     ┌──────────────────────────────┐
 │ autosync (every 2h)      │ ──────────▶ │ Caddy (HTTPS, free cert)     │
 │  · checks POLITEMall     │ /submissions│   └▶ Lastcall API + scheduler │
 │    with your saved login │             │        + Telegram bot        │
 └──────────────────────────┘             │        + SQLite (data/)      │
                                          └──────────────────────────────┘
```

Your POLITEMall login never leaves your laptop.

---

## 1. Create the server

1. Sign up at **cloud.oracle.com/free**. Choose **Singapore** as your home region
   (free servers can only be created there, and it can't be changed later).
2. **Compute → Instances → Create instance**
   - Name: `lastcall`
   - Image: **Canonical Ubuntu** (latest LTS)
   - Shape: an **Always Free-eligible** one: `VM.Standard.E2.1.Micro`, or
     `VM.Standard.A1.Flex` with 1 OCPU / 6 GB
   - Networking: create a new virtual cloud network with a **public subnet**, and
     **assign a public IPv4 address**
   - SSH keys: **Generate a key pair for me** → **Save private key**. Move the file to
     `C:\Users\paper\.ssh\lastcall.key`
3. **Create**, then copy the instance's **Public IP address**.

## 2. Open ports 80 and 443 (needed for HTTPS)

Instance page → click the **subnet** → **Security** / **Security Lists** → the default
list → **Add Ingress Rules** (twice):

| Source CIDR | IP Protocol | Destination port |
|---|---|---|
| `0.0.0.0/0` | TCP | `80` |
| `0.0.0.0/0` | TCP | `443` |

(The server's own firewall is opened by `install.sh` in step 4.)

## 3. Connect from your laptop

In Command Prompt (replace `IP` with your server's public IP):

```
icacls C:\Users\paper\.ssh\lastcall.key /inheritance:r /grant:r "%USERNAME%:R"
ssh -i C:\Users\paper\.ssh\lastcall.key ubuntu@IP
```

The first command fixes the key's permissions (SSH refuses keys others can read).
Type `yes` if asked to trust the server. You're now on the server.

## 4. Install Lastcall on the server

```
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/pinkfont/lastcall.git
cd lastcall
bash deploy/install.sh
```

The first run installs Docker, opens the firewall, creates `deploy/server.env` (with
`DOMAIN` already filled in), then stops and asks you to fill in the rest:

```
nano deploy/server.env
```

Copy `FEED_URL`, `API_KEY` and `TELEGRAM_BOT_TOKEN` from your laptop's `config\.env`.
Save with **Ctrl+O, Enter**, exit with **Ctrl+X**. **Don't run install.sh again yet.**

## 5. Bring your data over (keeps your bot owner, module names and done marks)

Open a **second** Command Prompt on your laptop:

```
scp -i C:\Users\paper\.ssh\lastcall.key C:\Users\paper\duetracker\data\duetracker.db ubuntu@IP:~/
```

Back on the server:

```
sudo cp ~/duetracker.db ~/lastcall/data/ && sudo chown 10001:10001 ~/lastcall/data/duetracker.db
```

## 6. Switch over

1. On your laptop, **close serve** (two bots on one token fight over messages).
2. On the server: `bash deploy/install.sh`
3. After a minute, open `https://DOMAIN/health` in a browser (DOMAIN is in
   `deploy/server.env`, e.g. `140-238-1-2.sslip.io`). It should show `{"status":"ok"}`.
4. In Telegram, tap **📋 Due**. It's now answered by the server.

Logs: `sudo docker compose -f deploy/compose.yaml logs -f lastcall` (Ctrl+C to stop).

## 7. Point your laptop at the server

1. In `config\.env` on your laptop, add: `API_URL=https://DOMAIN`
2. Double-click **autosync**. Every 2 hours while your laptop is on, it checks POLITEMall
   and pushes the results. The last run's output is in `data\laptop_sync.log`.
3. From now on, don't run **serve** on your laptop. **due** and **login** still work.

If your laptop goes quiet for 2 days, or the POLITEMall login expires, the bot tells you.

## Updating later

Push your changes to GitHub from your laptop as usual, then on the server:

```
cd ~/lastcall && bash deploy/update.sh
```

## Troubleshooting

| Problem | Check |
|---|---|
| `https://DOMAIN/health` doesn't load | Step 2 ingress rules; `sudo docker compose -f deploy/compose.yaml logs caddy` |
| Bot doesn't answer | Is serve still running on your laptop? Logs: `... logs lastcall` |
| Submissions never tick | `data\laptop_sync.log` on the laptop; `API_URL` and `API_KEY` must match the server |
| `Permission denied (publickey)` | Use `ubuntu@IP` and the `-i` key path from step 3 |
