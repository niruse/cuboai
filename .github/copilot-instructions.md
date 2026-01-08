# CuboAI Home Assistant Integration - Copilot Instructions

## Project Overview
This is an **unofficial Home Assistant custom component** for CuboAI baby monitors, distributed via HACS. It authenticates with CuboAI's cloud API using AWS Cognito SRP and exposes sensor entities for alerts, baby info, camera state, and subscription status.

**Key Features:**
- Multi-camera support (v1.1.0+) - multiple cameras in a single integration setup
- 2FA/MFA support (SMS and TOTP authenticator apps)
- Alert history with optional image downloads
- Camera state monitoring (online/offline)

## Repository Info
- **GitHub**: https://github.com/niruse/cuboai
- **Latest Release**: v1.1.0

## Architecture

### Component Structure
```
custom_components/cuboai/
├── __init__.py              # Entry setup, platform forwarding
├── config_flow.py           # Multi-step auth flow (Cognito SRP → MFA → camera selection)
├── sensor.py                # Four sensor classes inheriting CuboBaseSensor
├── const.py                 # Domain constant only
├── utils.py                 # Debug logging helper (disabled by default)
├── strings.json             # UI strings for config flow
├── translations/en.json     # English translations
└── api/cuboai_functions.py  # All CuboAI API interactions + token management

tests/
├── conftest.py              # Pytest fixtures (mock tokens, MFA challenges, cameras)
├── test_auth.py             # Authentication flow tests
├── test_config_flow.py      # Config flow integration tests
└── test_multi_camera.py     # Multi-camera support tests
```

### Key Patterns

**Multi-Camera Support**: The integration supports multiple cameras per account:
- Config flow fetches all cameras and allows user to select which to monitor
- Entry data stores `cameras` array: `[{"device_id": "...", "baby_name": "..."}]`
- Backward compatible with old single-camera entries via fallback to `device_id`/`baby_name` fields
- Per-camera sensors: BabyInfo, LastAlert, CameraState
- Per-account sensors: Subscription (shared across cameras)

**Sensor Base Class**: All sensors inherit from `CuboBaseSensor` which handles:
- Token persistence via `load_access_token()`/`save_access_token()` in `/config/` JSON files
- Automatic 401 retry with `_external_refresh_token()` pattern
- Async executor wrapping for blocking API calls

**Token Refresh Pattern** (follow this exactly for new API calls):
```python
await self._load_latest_tokens()
try:
    result = await self.hass.async_add_executor_job(api_function, self._access_token, ...)
except Exception as e:
    if "401" in str(e) or "Unauthorized" in str(e).lower():
        await self._external_refresh_token()
        result = await self.hass.async_add_executor_job(api_function, self._access_token, ...)
    else:
        raise
```

**API Authentication**: All CuboAI API calls require:
- Header: `x-cspp-authorization: Bearer {access_token}`
- Random User-Agent mimicking Android devices (see `generate_random_user_agent()`)

**Device Registry**: Each camera creates a device entry:
```python
@property
def device_info(self):
    return {
        "identifiers": {(DOMAIN, self._device_id)},
        "name": f"CuboAI {self._baby_name}",
        "manufacturer": "CuboAI",
        "model": "Baby Monitor",
    }
```

### Sensors Exposed
| Sensor Class | Unique ID Pattern | State Value | Per-Camera |
|-------------|-------------------|-------------|------------|
| `CuboBabyInfoSensor` | `cuboai_baby_info_{device_id}` | Baby name | Yes |
| `CuboLastAlertSensor` | `cuboai_last_alert_{device_id}` | Latest alert type | Yes |
| `CuboCameraStateSensor` | `cuboai_camera_state_{device_id}` | Camera online/offline | Yes |
| `CuboSubscriptionSensor` | `cuboai_subscription` | Subscription status | No (account-level) |

## API Endpoints Reference
- Login: `POST https://mobile-api.getcubo.com/v2/user/login`
- Token refresh: `POST https://mobile-api.getcubo.com/v1/oauth/token`
- Cameras: `GET https://api.getcubo.com/prod/user/cameras`
- Alerts: `GET https://api.getcubo.com/prod/timeline/alerts?since={ts}`
- Subscription: `GET https://api.getcubo.com/prod/services/v1/subscribed`
- Camera state: `GET https://api.getcubo.com/prod/camera/state?device_id={id}`

## Development Guidelines

### Adding New API Calls
1. Add function to `api/cuboai_functions.py`
2. Use `x-cspp-authorization` header pattern
3. Call from sensors via `hass.async_add_executor_job()` (blocking HTTP)
4. Implement 401 retry pattern in sensor's `async_update()`

### Adding New Sensors
1. Create class inheriting `CuboBaseSensor`
2. Implement `device_info` property linking to camera's `device_id`
3. Add to sensor creation loop in `async_setup_entry()` (per-camera or per-account)
4. Add unique_id with appropriate pattern

### Config Entry Data
Access via `entry.data` or `entry.options`:
- `cameras` (array of `{device_id, baby_name}` objects)
- `access_token`, `refresh_token`, `user_agent`
- Legacy: `device_id`, `baby_name` (for backward compatibility)
- Options: `download_images` (bool), `hours_back` (int), `alerts_count` (int)

### File Paths (hardcoded for HA container)
- Tokens: `/config/cuboai_access_token.json`, `/config/cuboai_refresh_token.json`
- Alert images: `/config/www/cuboai_images/` → web path `/local/cuboai_images/`
- Debug log: `/config/cuboai_auth.log`

### Dependencies
- `boto3` for AWS Cognito client
- `pycognito` for Cognito SRP authentication
- `requests` for HTTP calls

### Cognito Auth Constants
```python
CLIENT_ID = "1gvbkmngl920rtp6hlbp6057ue"
CLIENT_SECRET = "1ot7h8m3t83g0g4b7ais7ilcf12o44cvr9cbgad0t90kcpno56jr"
POOL_ID = "us-east-1_Wr7vffd5Y"
REGION = "us-east-1"
```

## Testing

### Running Tests
```bash
pip install -r requirements-test.txt
pytest
```

### Test Structure
- **conftest.py**: Shared fixtures for mock tokens, MFA challenges, camera data
- **test_auth.py**: Unit tests for Cognito SRP auth, MFA flows, error handling
- **test_config_flow.py**: Config flow step tests
- **test_multi_camera.py**: Multi-camera sensor creation and backward compatibility

### Key Fixtures
- `mock_tokens`: Standard Cognito token response
- `mock_mfa_challenge`: SMS MFA challenge response
- `mock_software_token_mfa_challenge`: TOTP MFA challenge
- `mock_cameras`: Sample multi-camera response

### Debug Logging
- `utils.py` logging is disabled by default (early return)
- Enable by removing `return` statement in `log_to_file()`
- Config flow logs to `/config/cuboai_auth.log`
