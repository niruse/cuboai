# 🍼 CuboAI Home Assistant Integration

Bring your CuboAI baby monitor into Home Assistant!  
Monitor alerts, camera status, subscription, and more—directly in your smart home dashboard.

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

## 🖥️ Example Lovelace Dashboard

Below is a sample of how you might present the alerts in a Markdown card, including event images:


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
