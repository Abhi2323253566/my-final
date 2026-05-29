# Auth Testing Playbook (Zoom Services Clone)

## Step 1: MongoDB Verification
- DB name from /app/backend/.env (DB_NAME)
- Verify admin user exists:
```
mongosh
use <DB_NAME>
db.users.findOne({email: "ppandit6926@gmail.com"})
```
- bcrypt hash must start with `$2b$`
- Indexes: users.email unique, login_attempts.identifier, password_reset_tokens.expires_at TTL

## Step 2: API Testing
```
curl -c cookies.txt -X POST $BACKEND_URL/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"ppandit6926@gmail.com","password":"alok@zoom123"}'
curl -b cookies.txt $BACKEND_URL/api/auth/me
curl -b cookies.txt -X POST $BACKEND_URL/api/auth/logout
```

## Step 3: Auth Error Render Check
- Wrong password should NOT crash React with FastAPI 422 errors.
- error mapper formatApiErrorDetail must format any object/array to a string.

## Step 4: Cookie + Bearer Both Work
- After login, cookies.txt has access_token and refresh_token
- /api/auth/me must work with cookies (no Authorization header)
