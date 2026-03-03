# Safe Shadow Backend - Railway Deployment Summary

## Status: READY FOR DEPLOYMENT (GitHub Integration Required)

### What Has Been Completed

#### 1. Railway Project Created ✓
- **Project ID**: `801465ba-cb54-489f-8bb1-da5ecb8d53ce`
- **Project Name**: `safe-shadow-backend`
- **API Token**: Valid and tested with Railway GraphQL API
- **Environment**: `production` (ID: `bbaf210c-8d60-4dca-85f9-2272971579d2`)

#### 2. Services Configured ✓
Three services have been created in the project:
- `backend` - Initial service placeholder (ID: `167b7701-0fd9-4986-b6a8-66ae490c2d19`)
- `api` - Docker Compose imported service (ID: `92634ddc-2c97-45a1-915c-edc0ea8dc038`)
- `api-fBfl` - Production-ready service (ID: `ca32818d-4019-43d1-ab49-c4fd581d9aae`)

#### 3. Docker Configuration ✓
- **Dockerfile**: Ready with Python 3.12-slim base image
- **docker-compose.yml**: Validated and imported into Railway
- **Health Check**: Configured at `/health` endpoint
- **Port**: 8000 (Railway-managed via `$PORT` env var)
- **Environment Variables**:
  - `PORT=8000` (managed by Railway)
  - `RATE_LIMIT_PER_MIN=30` (configurable)

#### 4. Application Code ✓
- **Framework**: FastAPI
- **Version**: 0.3.0
- **Build**: Latest commit `47360d7` with Railway-compatible configuration
- **Entry Point**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

### Why GitHub Integration Is Required

Railway's deployment pipeline supports two methods:
1. **GitHub Integration** (Primary) - Automatic deploys on push
2. **Railway CLI** (Alternative) - Requires browser-based interactive login

**Current Situation**:
- Railway CLI authentication fails in non-interactive environments
- Direct API deployment for source code is not exposed
- Docker Compose import works but requires a final trigger source

**Solution**: Push code to GitHub and link to Railway

### Complete Deployment Instructions

#### Step 1: Create a GitHub Repository

Option A: Create new repository
```bash
# Go to https://github.com/new
# Create repository: safe-shadow-app
```

Option B: Use existing repository
```bash
# If you have an existing repo, navigate to it
```

#### Step 2: Push Code to GitHub

```bash
cd /home/maxim/safe-shadow-app/backend

# Add GitHub as remote (if not already added)
git remote add origin https://github.com/YOUR_USERNAME/safe-shadow-app
# or update existing
git remote set-url origin https://github.com/YOUR_USERNAME/safe-shadow-app

# Push code
git branch -M main
git push -u origin main
```

#### Step 3: Connect GitHub to Railway (via Dashboard)

1. Go to Railway Dashboard: https://railway.app/dashboard
2. Select project: `safe-shadow-backend`
3. Click "Add" button
4. Select "GitHub Repo"
5. Authorize Railway application
6. Select your repository
7. Select branch: `main`
8. Choose service: `api-fBfl` (or create new)
9. Click "Deploy"

#### Step 4: Deploy via GraphQL API (Alternative)

Once code is on GitHub, trigger deployment:

```bash
curl -X POST https://backboard.railway.com/graphql/v2 \
  -H "Authorization: Bearer b667137f-30a9-44a9-b869-53e92a13c8b4" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "mutation { githubRepoDeploy(input: { projectId: \"801465ba-cb54-489f-8bb1-da5ecb8d53ce\", repo: \"YOUR_USERNAME/safe-shadow-app\", branch: \"main\", environmentId: \"bbaf210c-8d60-4dca-85f9-2272971579d2\" }) { id } }"
  }'
```

### Expected Results

After successful deployment:
- **Public URL**: `https://safe-shadow-backend-production.up.railway.app`
- **Health Check**: GET `/health` returns `{"ok": true, "version": "0.3.0"}`
- **Routes API**: POST `/routes` accepts route calculation requests

### Railway Project IDs (For Reference)

```json
{
  "project_id": "801465ba-cb54-489f-8bb1-da5ecb8d53ce",
  "project_name": "safe-shadow-backend",
  "environment_id": "bbaf210c-8d60-4dca-85f9-2272971579d2",
  "environment_name": "production",
  "service_id": "ca32818d-4019-43d1-ab49-c4fd581d9aae",
  "service_name": "api-fBfl",
  "api_token": "b667137f-30a9-44a9-b869-53e92a13c8b4",
  "api_endpoint": "https://backboard.railway.com/graphql/v2"
}
```

### API Endpoints (After Deployment)

```
GET https://safe-shadow-backend-production.up.railway.app/health
  Response: {"ok": true, "version": "0.3.0"}

POST https://safe-shadow-backend-production.up.railway.app/routes
  Content-Type: application/json
  
  Request:
  {
    "preset": "less_uv" | "cooler" | "fast",
    "origin": {
      "lat": number,
      "lon": number,
      "source": "search"
    },
    "destination": {
      "lat": number,
      "lon": number,
      "source": "search"
    },
    "departure_time": "2026-03-01T10:00:00+00:00",
    "walk_speed_mps": 1.35,
    "transport": "foot",
    "client": {
      "platform": "android",
      "app_version": "0.3.0",
      "device_id": "device123"
    }
  }
  
  Response:
  {
    "routes": [
      {
        "id": "route-1",
        "polyline": "...",
        "distance_m": 1234,
        "duration_s": 900,
        "metrics": {
          "temp_feels_avg_c": 22.5,
          "sun_minutes": 10,
          "shade_minutes": 5,
          "uv_dose": 4.2,
          "heat_load": 85.0,
          "confidence": "high"
        },
        "side_guidance": [...],
        "shade_map": "011001..."
      }
    ]
  }
```

### Troubleshooting

#### Issue: "Build failed" in Railway Dashboard
- Check that Dockerfile exists in root of repository
- Verify railway.toml settings
- Check build logs for specific errors

#### Issue: Application crashes after deployment
- Verify `/health` endpoint is accessible
- Check application logs in Railway Dashboard
- Ensure environment variables are set correctly
- Verify that external APIs (Overpass, OSRM) are accessible

#### Issue: Health check timeout
- Current timeout: 30 seconds
- If build takes longer, increase in railway.toml:
  ```toml
  [deploy]
  healthcheckTimeout = 60
  ```

#### Issue: CORS or connectivity errors
- Application has CORS enabled for all origins
- Verify that frontend is using correct backend URL
- Check browser console for specific error messages

### Deployment Verification Checklist

- [ ] GitHub repository created and code pushed to `main` branch
- [ ] Railway GitHub integration configured in Dashboard
- [ ] Deployment initiated and in progress
- [ ] Build logs show "Build successful"
- [ ] Health check passing (green status)
- [ ] Public HTTPS URL assigned and accessible
- [ ] `/health` endpoint returns `{"ok": true, ...}`
- [ ] POST `/routes` endpoint responds to requests
- [ ] No errors in Railway application logs

### Configuration Files

The backend is configured with:

**Dockerfile** (`/home/maxim/safe-shadow-app/backend/Dockerfile`):
- Base: Python 3.12-slim
- Installs: gcc, libgeos-dev (required for shapely)
- Copies: requirements.txt, app/
- Exposes: $PORT (8000 by default)
- CMD: uvicorn with proper host/port binding

**railway.toml** (`/home/maxim/safe-shadow-app/backend/railway.toml`):
- Builder: DOCKERFILE
- Start Command: uvicorn app.main:app --host 0.0.0.0 --port $PORT
- Health Check: /health
- Timeout: 30s
- Restart Policy: ON_FAILURE with 3 retries

**docker-compose.yml** (`/home/maxim/safe-shadow-app/backend/docker-compose.yml`):
- Service: api
- Build: Current directory (Dockerfile)
- Port mapping: 8000:8000
- Environment variables configured
- Health check enabled
- Restart: on-failure

### Additional Notes

1. **Rate Limiting**: Configured to 30 requests/minute by default, adjustable via `RATE_LIMIT_PER_MIN` environment variable

2. **External Dependencies**:
   - OpenStreetMap Routing (OSRM) - for route calculation
   - Overpass API - for building data
   - OpenWeatherMap - for weather data (if needed)
   All accessed via public HTTP APIs

3. **Performance**:
   - Buildings cache: 10 minutes (reduces API calls)
   - Route analysis: Supports up to 5 route variants
   - Concurrent processing: ThreadPoolExecutor with 4 workers

4. **Monitoring**:
   - Railway Dashboard provides real-time logs
   - Health endpoint for uptime monitoring
   - Automatic restarts on failure

### Support & Documentation

- Railway Documentation: https://docs.railway.app/
- FastAPI Documentation: https://fastapi.tiangolo.com/
- OpenStreetMap Services: https://www.openstreetmap.org/

---

**Summary**: The Railway project infrastructure is fully configured and ready. The only remaining step is to push the code to GitHub and complete the GitHub integration in the Railway Dashboard or via the GraphQL API.

**Expected URL**: https://safe-shadow-backend-production.up.railway.app
