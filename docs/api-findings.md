# CuboAI API Exploration Findings

**Date:** January 8, 2026  
**Explored by:** API exploration script  
**Cameras tested:** CB022050E7BA61F8 (Phoebe), SW0528D043A0E429 (Oscar)

## Summary

The CuboAI API is split between two main hosts:
- `https://api.getcubo.com/prod/` - Main API for data retrieval
- `https://mobile-api.getcubo.com/` - Authentication only (login, token refresh)

Camera control (lullabies, nightlight, etc.) appears to **NOT** be available via REST API. These controls likely use direct device communication via:
- AWS IoT MQTT
- P2P/WebRTC for streaming
- Device credentials provided in `/user/cameras` response

---

## Working Endpoints

### Authentication

| Endpoint | Method | Description |
|----------|--------|-------------|
| `https://mobile-api.getcubo.com/v2/user/login` | POST | Login with Cognito SRP |
| `https://mobile-api.getcubo.com/v1/oauth/token` | POST | Refresh access token |

### User & Camera Data

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/prod/user/cameras` | GET | List all cameras with profiles and settings |
| `/prod/camera/state?device_id={id}` | GET | Camera online/offline state |
| `/prod/lullabies` | GET | List all available lullaby songs |
| `/prod/timeline/alerts?since={ts}` | GET | Alert history with images |
| `/prod/services` | GET | Service/subscription data |
| `/prod/services/v1/subscribed` | GET | Subscription status per camera |

---

## Endpoint Details

### GET /prod/user/cameras

Returns comprehensive camera and user data:

```json
{
  "data": [
    {
      "device_id": "CB022050E7BA61F8",
      "dev_admin_id": "admin@CB022050E7BA61F8",
      "dev_admin_pwd": "Yl4aJ-N?l2dC",
      "created": "2023-02-19T16:49:02.000Z",
      "license_id": "NUGA7PZHEFZ9JTM4111A",
      "role": "admin",
      "user_id": "5d630d8d-0424-4bc0-a0de-f1ffafab4f27",
      "tag": 3,
      "settings": "{\"alexa_enable\": true}"
    }
  ],
  "profiles": [
    {
      "device_id": "CB022050E7BA61F8",
      "user_id": "...",
      "profile": "{\"baby\": \"Phoebe\", \"birth\": \"2022-10-03\", \"avatar\": \"https://...\", \"gender\": 1}"
    }
  ],
  "report_settings": [
    {
      "enable": 1,
      "device_id": "CB022050E7BA61F8",
      "time_zone": "Europe/London",
      "sleep_time": "19:00",
      "wakeup_time": "08:00",
      "report_time": 9,
      "gmt_offset": 0
    }
  ]
}
```

**Key fields:**
- `dev_admin_id` / `dev_admin_pwd` - Device admin credentials (possibly for P2P/local access)
- `settings` - JSON string with Alexa enable flag
- `profile` - JSON string with baby name, birth date, avatar URL, gender (0=male, 1=female)
- `report_settings` - Sleep report configuration

### GET /prod/camera/state?device_id={id}

```json
{
  "ts": "1767906872.88",
  "state": "online"
}
```

### GET /prod/lullabies

Returns full song catalog (version 7):

```json
{
  "v": 7,
  "modified_ts": 1638265814,
  "songs": [
    {
      "id": "CC2D07C1-86AA-482E-A7E2-2DF09A1E3B0F",
      "title": "Morning Rain Forest",
      "en_title": "Morning Rain Forest",
      "zh_title": "大自然：叢林",
      "fr_title": "Matin forêt tropicale",
      "de_title": "Morgendlicher Regenwald",
      "ja_title": "朝のジャングル",
      "category": "noise",
      "tag": "built_in",
      "url": "",
      "sha1": "a9ec416d5332010189952ec084d6201c203851db",
      "time_length": 51,
      "size": 1231978,
      "is_built_in": true,
      "is_expert": false
    }
  ]
}
```

**Categories:** `noise`, `light`, `lullabies`

**Known songs:**
| ID | Title | Category |
|----|-------|----------|
| CC2D07C1-86AA-482E-A7E2-2DF09A1E3B0F | Morning Rain Forest | noise |
| 0B720264-E7E6-4050-8A81-27EDEC01E172 | Rain | noise |
| 963B384F-689F-4669-90AD-A35ED9C4125C | Birds | noise |
| 4C89C060-B51E-4E02-8C9C-32C3E03D023C | Gentle Music Box Melody | light |
| C067C97C-DAD2-413B-B55C-A60437B58D04 | Are You Sleeping | lullabies |
| 27BF885A-6B28-4B64-924B-43DFAE08D45F | Twinkle Twinkle Little Star | lullabies |

### GET /prod/timeline/alerts

Supports multiple query parameters:
- `since={timestamp}` - Unix timestamp to fetch alerts from (required, use 0 for all)
- `device_id={id}` - Filter by specific camera (optional - returns all cameras if omitted)
- `limit={n}` or `count={n}` - Limit number of results (both work)
- `page={n}&per_page={n}` - Pagination support

```json
{
  "data": [
    {
      "id": "CB022050E7BA61F8-1767381087-7ff",
      "device_id": "CB022050E7BA61F8",
      "image": "https://eu-storage.getcubo.com/file/.../alert/20260102/env_1767381087_thumb.jpeg",
      "type": "CUBO_ALERT_TEMPERATURE",
      "ts": 1767381087,
      "params": "{\"setting\": \"20.000000:25.000000\", \"temperature\": \"19\"}",
      "user_id": "...",
      "created": "2026-01-02T19:11:33.000Z",
      "feedback": null,
      "feedback_reason": null,
      "feedback_time": null,
      "profile": "{\"baby\": \"Phoebe\", ...}",
      "region": "europe-west4"
    }
  ]
}
```

**Alert types observed:**
- `CUBO_ALERT_TEMPERATURE` - Temperature out of range

**Temperature params format:**
```json
{
  "setting": "20.000000:25.000000",  // min:max range
  "temperature": "19"                 // current temp in Celsius
}
```

### GET /prod/services/v1/subscribed

```json
{
  "code": 0,
  "result": [
    {
      "id": 277244,
      "user_id": "...",
      "service_id": "Premium",
      "status": "active",
      "kind": "free",
      "device_id": "SW0528D043A0E429",
      "service_start_date": "2025-07-19T15:22:32.000Z",
      "service_end_date": "2026-07-19T15:22:32.000Z",
      "platform": "cubo_app"
    }
  ]
}
```

**Service IDs:** `Premium`, `L1`, `Basic`

---

## Endpoints That Return 401 (Need Different Auth)

These endpoints exist but require a different authentication mechanism:

| Endpoint | Response | Notes |
|----------|----------|-------|
| `/prod/camera/lullabies?device_id={id}` | "No API key specified" | Requires `x-api-key` header (not access token) |
| `/prod/ping` | "No API key specified" | Health check, requires API key |

Tried adding `x-api-key: {access_token}` header but still got 401. These endpoints likely require a separate API key that's embedded in the mobile app or retrieved from another endpoint.

---

## Alert Query Parameters (discovered in exploration)

The `/prod/timeline/alerts` endpoint supports these parameters:

| Parameter | Description | Example |
|-----------|-------------|---------|
| `since` | Unix timestamp to fetch from (required) | `since=0` for all |
| `device_id` | Filter by camera (**NOTE: doesn't actually filter!**) | `device_id=ABC123` |
| `type` | Filter by alert type | `type=CUBO_ALERT_TEMPERATURE` |
| `limit` / `count` | Max results (both work) | `limit=100` |
| `page` / `per_page` | Pagination | `page=1&per_page=50` |

**Important:** The `device_id` parameter on alerts does NOT filter results - it returns all alerts for all cameras regardless of the value passed.

---

## Endpoints That Return 404 (Don't Exist)

All of these returned 404 - the API doesn't expose them:

### Camera Control (Not Available via REST)
- `/prod/camera/lullaby` (POST)
- `/prod/camera/control`
- `/prod/camera/command`
- `/prod/camera/nightlight`
- `/prod/lullaby/play`
- `/prod/lullaby/stop`
- `/prod/command`
- `/prod/control`

### IoT / Real-time (Not Available via REST)
- `/prod/iot/*`
- `/prod/mqtt`
- `/prod/ws`
- `/prod/socket`
- `/prod/p2p`
- `/prod/webrtc`
- `/prod/streaming`

### Camera Data (Not Available)
- `/prod/camera/info`
- `/prod/camera/settings`
- `/prod/camera/config`
- `/prod/camera/temperature`
- `/prod/camera/environment`
- `/prod/camera/stream`

### Reports / Analytics (Not Available via REST)
- `/prod/report/*`
- `/prod/sleep/*`
- `/prod/analytics`
- `/prod/stats`

### User Data (Not Available)
- `/prod/user/profile`
- `/prod/user/info`
- `/prod/user/settings`
- `/prod/user/alerts`
- `/prod/user/reports`

### Other
- `/prod/firmware`
- `/prod/ota`
- `/prod/sharing`
- `/prod/invites`

---

## API Authentication

All requests require:
```
x-cspp-authorization: Bearer {access_token}
User-Agent: okhttp/5.0.0-alpha.14  (or similar Android user agent)
Content-Type: application/json
Accept-Encoding: gzip
```

---

## Conclusions

### What CAN be implemented via REST API:
1. ✅ **Camera list and baby profiles** - Full info available
2. ✅ **Camera online/offline state** - Available
3. ✅ **Alert history with images** - Available, includes temperature data
4. ✅ **Subscription status** - Available per camera
5. ✅ **Lullaby catalog** - Full song list available (read-only)
6. ✅ **Temperature from alerts** - Extractable from `CUBO_ALERT_TEMPERATURE` params
7. ✅ **Sleep schedule settings** - From `report_settings` in cameras response

### What CANNOT be implemented via REST API:
1. ❌ **Lullaby play/pause/select** - No control endpoint found (likely MQTT)
2. ❌ **Nightlight control** - No endpoint found
3. ❌ **Live video streaming** - Requires P2P (Tutk/Kalay SDK)
4. ❌ **Real-time temperature** - Only from alerts when threshold crossed
5. ❌ **Sleep reports/analytics** - Not exposed via API
6. ❌ **Alert type filtering by device** - Parameter exists but doesn't filter

### Explored but Not Found (404)

**Hundreds of endpoints tested across multiple rounds:**
- `/prod/iot/*`, `/prod/mqtt/*`, `/prod/ws/*` - IoT/real-time
- `/prod/camera/control`, `/prod/camera/command` - Device control
- `/prod/nightlight`, `/prod/light` - Light control
- `/prod/p2p/*`, `/prod/webrtc/*`, `/prod/stream/*` - Streaming
- `/prod/tutk/*`, `/prod/kalay/*` - P2P SDK
- `/prod/report/*`, `/prod/sleep/*`, `/prod/analytics/*` - Reports
- `/prod/family/*`, `/prod/shares/*`, `/prod/sharing/*` - Sharing
- All `/v1/`, `/v2/`, `/v3/` version prefixes
- Alternative hosts: `iot.getcubo.com`, `stream.getcubo.com`, `eu-api.getcubo.com` (don't exist)

---

## Technical Architecture Insights

Based on exploration, CuboAI appears to use:

1. **REST API** (`api.getcubo.com/prod/`) - Read-only data access
2. **Mobile API** (`mobile-api.getcubo.com`) - Authentication only
3. **AWS Cognito** - User authentication (us-east-1_Wr7vffd5Y)
4. **Likely P2P SDK** (Tutk/Kalay) - Video streaming (based on common baby monitor patterns)
5. **Likely MQTT/AWS IoT** - Device commands (lullabies, nightlight)
6. **EU Storage** (`eu-storage.getcubo.com`) - Image/media files

The `dev_admin_id` and `dev_admin_pwd` in the cameras response may be:
- P2P connection credentials for direct device access
- Local network connection credentials
- These could potentially enable local control without cloud

---

## Next Steps for Lullaby Control (Issue #3)

The lullaby control likely requires one of:

### Option A: AWS IoT MQTT (Most Likely)
- The app probably publishes commands to an MQTT topic
- Would need to reverse engineer the mobile app to find:
  - AWS IoT endpoint
  - MQTT topic structure (e.g., `cubo/{device_id}/command`)
  - Message format for play/stop commands
- Would require implementing MQTT client in Home Assistant

### Option B: P2P SDK (Tutk/Kalay)
- Common in baby monitors for video + commands
- The `dev_admin_id`/`dev_admin_pwd` credentials may be for this
- Would require implementing Tutk SDK integration (complex)

### Option C: Local Network API
- Device may expose local HTTP/CoAP API on LAN
- Could try scanning for open ports on camera IP
- Would require device discovery and local API reverse engineering

### Recommended Approach
1. Capture mobile app traffic with mitmproxy/Charles while playing lullabies
2. Look for MQTT connections to AWS IoT endpoints
3. Document the message format
4. Implement using `aiomqtt` or similar in HA

---

## Potential New Features Based on Findings

### High Priority (Easy to Implement)
1. **Temperature sensor** - Extract from `CUBO_ALERT_TEMPERATURE` alert params
   - State: last temperature reading (e.g., "19°C")
   - Attributes: threshold settings, alert timestamp
   
2. **Alert count sensor** - Number of alerts in last 24h/week
   - Already have the data, just need to count

3. **More baby info attributes** - Expose from profile JSON:
   - Birth date
   - Gender
   - Avatar URL

### Medium Priority
4. **Sleep schedule sensor** - From `report_settings`:
   - Sleep time, wakeup time
   - Report enabled status
   - Timezone

5. **Lullaby catalog select** - Read-only list of available songs
   - Could be used in HA automations to reference song IDs
   - Prep work for when control is figured out

### Low Priority (Requires More Work)
6. **Device credentials entity** - For advanced users:
   - `dev_admin_id`, `dev_admin_pwd`
   - Could enable local integration development

---

## Raw API Responses Archive

### Full lullaby list (truncated in exploration)
Categories: noise, light, lullabies
Total songs: ~15+ built-in songs across categories

### Services response
Shows both `/services` and `/services/v1/subscribed` return similar data with subscription details per camera.
