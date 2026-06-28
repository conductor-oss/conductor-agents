// Deliberately vulnerable sample app — for testing security-conductor's SAST
// + route-extraction only. DO NOT deploy.
const express = require('express');
const { exec } = require('child_process');
const app = express();
app.use(express.json());

// Hardcoded secrets (secret scanners should flag these)
const AWS_ACCESS_KEY = "AKIAZ3XK7QP2WM9NT4RB";
const GITHUB_TOKEN = "ghp_1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz";
const DB_PASSWORD = "SuperSecret123!";

let db, users;

app.get('/', (req, res) => res.send('home'));

app.get('/search', (req, res) => {
  // SQL injection via string concatenation
  const sql = "SELECT * FROM products WHERE name = '" + req.query.q + "'";
  db.query(sql, (e, r) => res.json(r));
});

app.get('/ping', (req, res) => {
  // OS command injection
  exec('ping -c 1 ' + req.query.host, (e, out) => res.send(out));
});

app.post('/calc', (req, res) => {
  // Code injection
  const result = eval(req.body.expr);
  res.send(String(result));
});

app.get('/admin/users', (req, res) => res.json(users));        // sensitive, no authz
app.post('/login', (req, res) => res.json({ ok: true }));
app.put('/account/:id', (req, res) => res.json({ id: req.params.id }));

app.listen(process.env.PORT || 3000);
