# 🍼 CuboAI Home Assistant Integration

Bring your CuboAI baby monitor into Home Assistant!  
Monitor alerts, camera status, subscription, and more—directly in your smart home dashboard.

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
- Support for multiple CuboAI cameras (multi-instance integration)
- Easy authentication with CuboAI (uses warrant for SRP/AWS Cognito)
- All data stays local—no cloud polling from Home Assistant servers

---

## 🛠️ Installation

### 📦 Installation via HACS

1. Go to **HACS** in Home Assistant
2. Click the **three dots menu > Custom repositories**
3. Add this repository URL:  
https://github.com/niruse/cuboai
<img width="257" height="139" alt="image" src="https://github.com/user-attachments/assets/c5cb26a9-029e-45db-b05b-e75e5cd146f4" />

yaml
Copy
Edit
4. Select **Integration** as category
5. Search for **CuboAI** in HACS and click **Install**
6. **Restart Home Assistant**


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

```yaml
type: markdown
title: 🍼 CuboAI Last 5 Alerts
content: |
  {% set alerts = state_attr('sensor.cuboai_last_alert_yourbaby', 'alerts') %}
  {% if alerts %}
  | Type | Time | Image |
  |------|------|-------|
  {% for alert in alerts %}
  | **{{ alert['type'].replace('CUBO_ALERT_','').replace('_',' ').title() }}** | {{ alert['created'][:16].replace('T',' ') }} | {% if alert['image'] %}![img]({{ alert['image'] }}){% else %}-{% endif %} |
  {% endfor %}
  {% else %}
  _No recent alerts_
  {% endif %}
