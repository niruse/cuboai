# 🍼 CuboAI Home Assistant Integration

[![CI](https://github.com/niruse/cuboai/actions/workflows/ci.yml/badge.svg)](https://github.com/niruse/cuboai/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/niruse/cuboai/branch/main/graph/badge.svg)](https://codecov.io/gh/niruse/cuboai)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Bring your CuboAI baby monitor into Home Assistant!  
Monitor alerts, camera status, subscription, and more—directly in your smart home dashboard.

---

## ☕ Support

If you found this project helpful, you can [buy me a coffee](https://coff.ee/niruse)!

---

## 🚨 Disclaimer

> **Warning:**  
> This is an unofficial integration.  
> You are fully responsible for the use of your credentials and your data.  
> The author and contributors take no responsibility for any issues, account restrictions, or data loss that may occur.  
>  
> Use at your own risk.

---

## ✨ Features

- Sensor for **baby info** (name, gender, birth date)
- Sensor for **last CuboAI alerts** (with image thumbnails)
- Optionally **download alert images** locally for fast, private display
- Sensor for **subscription status** (Premium, trial, grace period, etc.)
- Sensor for **camera online/offline state**
- **[NEW] Local Control & Live Features** via direct LAN connection:
  - **Live Detection Sensors**: Cry Detection, Cough Detection, and Sleep Safety (covered face/rollover)
  - **Night Light Control**: Native Home Assistant brightness slider for the night light (1% - 100%)
  - **Lullaby Controls**: Play/stop lullabies directly from Home Assistant
  - **Status LED Switch**: Toggle the physical status indicator LED on the camera
  - **Sleep Mode Switch**: Turn the camera's sleep mode on/off
  - **Environmental Sensors**: Live Temperature and Humidity readings
  - **Firmware Version**: Display the camera's active firmware version
  - **JPEG Snapshot**: Display live camera snapshot in Home Assistant via go2rtc
- Support for multiple CuboAI cameras (multi-instance integration)
- Easy authentication with CuboAI (uses pycognito for SRP/AWS Cognito)

---

## 🛠️ Installation

### 📦 Installation via HACS

1. Go to **HACS** in Home Assistant.
2. Click the **three dots menu** (⋮) > **Custom repositories**.
3. Add this repository URL:  
   `https://github.com/niruse/cuboai`
4. In the **Category** dropdown, select **Integration**.
5. Click **Add**.<br>
   <img width="257" height="139" alt="HACS Custom Repository" src="https://github.com/user-attachments/assets/c5cb26a9-029e-45db-b05b-e75e5cd146f4" />
6. Search for **CuboAI** in HACS and click **Install**.
7. **Restart Home Assistant** to complete the installation.

---

### 📁 Manual Installation

1. Download the `cuboai` folder from this repository
2. Place it in `/config/custom_components/` on your Home Assistant instance
3. Restart Home Assistant

---

## Sample Images of Sensors

Here are example screenshots from the CuboAI integration:

### Last 5 Alerts Sensor Card

![CuboAI Alerts Example](https://github.com/user-attachments/assets/ea368a6b-ca80-4f08-9160-898309fcd0f0)

### Camera State & Subscription Status

![CuboAI Camera and Status Example](https://github.com/user-attachments/assets/eb5eca1e-ccf1-4ed4-b6e0-f4defc56641d)

![CuboAI Camera and Subscription Example](https://github.com/user-attachments/assets/0ac518f7-e24e-471e-b550-dcf928ab6ddc)

## Baby Info
![CuboAI Camera and Subscription Example](https://github.com/user-attachments/assets/3f8d49bf-38b3-41e9-9f41-6c7f63563c8d)

## 🖥️ Example Lovelace Dashboard

Below is a sample of how you might present the alerts in a Markdown card, including event images:
![CuboAI Dashboard sample](https://github.com/user-attachments/assets/4acccaf6-451e-4b34-96bd-e97271ebb800)

> 💡 Replace `{{Your Baby Name}}` with the actual entity suffix (e.g., `nir`).

```yaml
type: markdown
title: 🍼 CuboAI Last 5 Alerts
content: >
  {% set alerts = state_attr('sensor.cuboai_last_alert_{{Your Baby Name}}', 'alerts') %}

  {% if alerts %}

  | Type | Time | Image |

  |------|------|-------|

  {% for alert in alerts %}

  | **{{ alert['type'].replace('CUBO_ALERT_','').replace('_',' ').title() }}**
  | 
    {{ as_timestamp(alert['created']) | timestamp_custom('%Y-%m-%d %H:%M', true) }} | 
    {% if alert['image'] %}![img]({{ alert['image'] }}){% else %}-{% endif %} |
  {% endfor %}

  {% else %}

  _No recent alerts_

  {% endif %}

```
---

## 🎤 Two-Way Audio (Microphone Support)

Home Assistant's default camera card does **not** support two-way audio or microphone buttons out of the box. To see the microphone button and use the 2-way talk feature, you need to use the **WebRTC Camera** custom Lovelace card (by AlexxIT), which you can install via HACS.

Here is how to get the microphone button on your dashboard:

1. **Install WebRTC Camera:** Go to HACS -> Frontend -> Search for "WebRTC Camera" and install it, then reload your browser.
2. **Add to Dashboard:** Go to your dashboard, click "Edit Dashboard", and add a "Custom: WebRTC Camera" card.
3. **Configure the Card:** Set the URL to your camera's exact go2rtc stream ID. You can easily find this ID by looking at the state of the `sensor.cuboai_webrtc_stream_{baby_name}` entity that this integration creates (e.g. `cuboai_YOUR_CAMERA_ID`).

Your card configuration in YAML should look like this:

```yaml
type: custom:webrtc-camera
url: cuboai_YOUR_CAMERA_ID
media: video,audio,microphone
```

*(Note: Setting `media: video,audio,microphone` is what explicitly tells the WebRTC card to render the Microphone button on the screen!)*

Once you save that card, you will see a microphone icon directly over the video feed. When you click and hold it, it will stream your PC/phone microphone audio directly through the PureSession backchannel to the camera!

---

## 🎨 CuboAI Custom Lovelace Card

For the absolute best experience, we provide a **Custom Lovelace Card** (`cuboai-card.js`) that automatically wraps the WebRTC Camera card and adds:
- **Live Environmental Overlays**: Real-time Temperature & Humidity floating directly over the video feed!
- **Baby Vitals**: Live BPM (Heart Rate) overlay directly on the video if you have the Sleep Sensor Pad!
- **Microphone Toggle**: A beautiful, floating microphone button for two-way audio.
- **Smart Fallback**: Automatically leverages the camera entity to enable automatic fallback to MSE when you are outside your home network (so video always plays flawlessly over Home Assistant Cloud / Nabu Casa)!

### 🛠️ Installing the Custom Card

If you installed this integration manually or via HACS, the `cuboai-card.js` file is already located in the `www/` folder of the integration.

1. In Home Assistant, navigate to **Settings** -> **Dashboards** -> **Resources** (You may need to click the 3 dots in the top right to see Resources).
2. Click **Add Resource**.
3. Set the URL to: `/local/cuboai-card.js?v=1`
4. Set the Resource Type to: **JavaScript Module**.
5. Click **Create**!

### ⚠️ Important: Bypassing the Home Assistant Cache!

If you ever update the `cuboai-card.js` file, you will notice that the browser **will not** load the new version. This is because Home Assistant's internal web server aggressively caches `/local/` resources using the version string in the URL.

**To forcefully update the card after applying fixes:**
1. Go back to **Settings** -> **Dashboards** -> **Resources**.
2. Click on `/local/cuboai-card.js?v=1`.
3. Change the version number (e.g., change `?v=1` to `?v=2` or `?v=99`).
4. Click **Update**.
5. Do a hard refresh in your browser (Ctrl+F5) or use an Incognito Window!

---

## 🤝 Contributing

We welcome:
- 🔧 Bug fixes
- 🌟 Features
- 🧠 Suggestions

Submit a PR or [open an issue](https://github.com/niruse/cuboai/issues)
