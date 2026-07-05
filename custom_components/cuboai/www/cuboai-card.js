// Global patch to prevent badly written third-party cards (like searchable-list-card) from crashing the entire HA dashboard
if (!window._cuboai_registry_patched) {
  window._cuboai_registry_patched = true;
  const originalDefine = customElements.define;
  customElements.define = function(name, constructor, options) {
    if (!customElements.get(name)) {
      try {
        originalDefine.call(this, name, constructor, options);
      } catch (e) {
        console.warn(`[CuboAI Patch] Suppressed error registering custom element ${name}:`, e);
      }
    } else {
      console.warn(`[CuboAI Patch] Prevented duplicate registration of custom element: ${name}`);
    }
  };
}

class CuboAICameraCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._rendered) {
      this._rendered = true;
      this.render();
    }
  }

  render() {
    if (!this._hass) return;

    const cameras = [];
    for (const entity_id in this._hass.states) {
      if (entity_id.startsWith('media_player.') && entity_id.endsWith('_speaker')) {
        const state = this._hass.states[entity_id];
        if (state.attributes && state.attributes.device_id) {
          const name = state.attributes.friendly_name || entity_id;
          cameras.push({ entity_id, name: name.replace(' Speaker', ''), deviceId: state.attributes.device_id });
        }
      }
    }

    let optionsHtml = '<option value="">Auto-Detect (First Camera)</option>';
    optionsHtml += cameras.map(cam => 
      `<option value="${cam.deviceId}">${cam.name}</option>`
    ).join('');

    if (cameras.length === 0) {
      optionsHtml = `<option value="" disabled>No CuboAI cameras found</option>`;
    }

    this.innerHTML = `
      <div class="card-config">
        <label for="camera-select" style="display: block; font-weight: 500; margin-bottom: 8px;">Select Camera:</label>
        <select id="camera-select" style="width: 100%; padding: 8px; border-radius: 4px; border: 1px solid var(--divider-color, #ccc); background: var(--card-background-color, #fff); color: var(--primary-text-color, #000); margin-bottom: 16px;">
          ${optionsHtml}
        </select>
        
        <label for="mute-select" style="display: block; font-weight: 500; margin-bottom: 8px;">Initial Audio State:</label>
        <select id="mute-select" style="width: 100%; padding: 8px; border-radius: 4px; border: 1px solid var(--divider-color, #ccc); background: var(--card-background-color, #fff); color: var(--primary-text-color, #000); margin-bottom: 16px;">
          <option value="remember">Remember Last State (Default)</option>
          <option value="muted">Always Start Muted</option>
          <option value="unmuted">Always Start Unmuted</option>
        </select>

        <label for="song-filter-select" style="display: block; font-weight: 500; margin-bottom: 8px;">Default Song Filter:</label>
        <select id="song-filter-select" style="width: 100%; padding: 8px; border-radius: 4px; border: 1px solid var(--divider-color, #ccc); background: var(--card-background-color, #fff); color: var(--primary-text-color, #000); margin-bottom: 16px;">
          <option value="all">All Users</option>
          <option value="me">My Songs</option>
        </select>
        
        <label for="playlist-filter-select" style="display: block; font-weight: 500; margin-bottom: 8px;">Default Playlist Filter:</label>
        <select id="playlist-filter-select" style="width: 100%; padding: 8px; border-radius: 4px; border: 1px solid var(--divider-color, #ccc); background: var(--card-background-color, #fff); color: var(--primary-text-color, #000);">
          <option value="all">All Users</option>
          <option value="me">My Playlists</option>
        </select>

        <p style="color: var(--secondary-text-color); font-size: 12px; margin-top: 12px;">
          Note: By default, the card will automatically find and display the first camera on your account. 
          Use the dropdown above if you have multiple cameras and want to pin this card to a specific one.
        </p>
      </div>
    `;
    
    const selectEl = this.querySelector('#camera-select');
    if (selectEl) {
      selectEl.value = this._config ? (this._config.device_id || '') : '';
      selectEl.addEventListener('change', (e) => this._valueChanged(e));
    }
    
    const muteSelect = this.querySelector('#mute-select');
    if (muteSelect) {
      muteSelect.value = this._config ? (this._config.default_mute_state || 'remember') : 'remember';
      muteSelect.addEventListener('change', (e) => this._valueChanged(e));
    }
    
    const songFilter = this.querySelector('#song-filter-select');
    if (songFilter) {
      songFilter.value = this._config ? (this._config.default_song_filter || 'all') : 'all';
      songFilter.addEventListener('change', (e) => this._valueChanged(e));
    }
    
    const plFilter = this.querySelector('#playlist-filter-select');
    if (plFilter) {
      plFilter.value = this._config ? (this._config.default_playlist_filter || 'all') : 'all';
      plFilter.addEventListener('change', (e) => this._valueChanged(e));
    }
  }

  _valueChanged(ev) {
    if (!this._config || !this._hass) return;
    const target = ev.target;
    
    let newConfig = { ...this._config };
    if (target.id === "camera-select") {
      newConfig.device_id = target.value;
    } else if (target.id === "mute-select") {
      newConfig.default_mute_state = target.value;
    } else if (target.id === "song-filter-select") {
      newConfig.default_song_filter = target.value;
    } else if (target.id === "playlist-filter-select") {
      newConfig.default_playlist_filter = target.value;
    }

    const event = new Event("config-changed", { bubbles: true, composed: true });
    event.detail = { config: newConfig };
    this.dispatchEvent(event);
  }
}

if (!customElements.get('cuboai-camera-card-editor')) {
  customElements.define("cuboai-camera-card-editor", CuboAICameraCardEditor);
}


class CuboAICameraCard extends HTMLElement {
  
  static getConfigElement() {
    return document.createElement("cuboai-camera-card-editor");
  }

  static getStubConfig() {
    return { type: "custom:cuboai-camera-card", device_id: "" };
  }

  set hass(hass) {
    if (this._error) {
      this.innerHTML = `<div style="background: #fee; border: 1px solid #fcc; color: #c00; padding: 15px; border-radius: 8px;"><h3>CuboAI Card Configuration Error</h3><p>${this._error.message}</p><pre>${this._error.stack}</pre></div>`;
      return;
    }
    try {
      if (!hass || !hass.states) return;
      this._hass = hass;
      
      let deviceId = this._config?.device_id;
      this._speakerEntityId = null;
      this._lullabyEntityId = null;
      
      for (const entity_id in hass.states) {
        if (entity_id.startsWith('media_player.') && entity_id.endsWith('_speaker')) {
          const state = hass.states[entity_id];
          if (state.attributes && state.attributes.device_id) {
            if (!deviceId || deviceId === state.attributes.device_id) {
              deviceId = state.attributes.device_id;
              this._speakerEntityId = entity_id;
              break;
            }
          }
        }
      }

      if (this._speakerEntityId) {
        const candidate = this._speakerEntityId.replace('_speaker', '_lullaby');
        if (hass.states[candidate]) {
          this._lullabyEntityId = candidate;
        } else {
          // Fallback search
          for (const entity_id in hass.states) {
            if (entity_id.startsWith('media_player.') && entity_id.endsWith('_lullaby')) {
              this._lullabyEntityId = entity_id;
              break;
            }
          }
        }
      }

      if (!this.content) {
        this.style.display = 'block';
        this.style.position = 'relative';
        if (!deviceId || !this._speakerEntityId) return;

      this.micEnabled = false;
      
      const savedMuted = localStorage.getItem(`cuboai_muted_${deviceId}`);
      const defaultMuteState = this._config?.default_mute_state || 'remember';
      
      if (defaultMuteState === 'unmuted') {
        this.isMuted = false;
      } else if (defaultMuteState === 'muted') {
        this.isMuted = true;
      } else {
        this.isMuted = savedMuted ? savedMuted === 'true' : true;
      }

      let babyName = null;
      if (this._speakerEntityId) {
          const nameParts = this._speakerEntityId.replace('media_player.', '').replace('_speaker', '').split('_');
          babyName = nameParts[nameParts.length - 1]; // e.g. "suwon"
      }
      
      let webrtcEntity = null;
      let rtspPort = 8555;
      for (const entity_id in hass.states) {
          if (entity_id.startsWith('camera.cuboai_') && entity_id.endsWith('_local_camera')) {
              if (babyName && !entity_id.includes(babyName)) continue;
              webrtcEntity = entity_id;
              if (hass.states[entity_id].attributes && hass.states[entity_id].attributes.rtsp_port) {
                  rtspPort = hass.states[entity_id].attributes.rtsp_port;
              }
              break;
          }
      }

      const webrtcConfig = {
        type: 'custom:webrtc-camera',
        entity: webrtcEntity || '',
        url: webrtcEntity ? undefined : `rtsp://127.0.0.1:${rtspPort}/cuboai_combined_${deviceId}`,
        mode: (navigator.vendor && navigator.vendor.includes('Apple')) ? 'mp4,hls,mse' : 'webrtc,mse',
        ui: true,
        muted: this.isMuted,
        media: this.micEnabled ? 'video,audio,microphone' : 'video,audio'
      };
      
      // Add the microphone overlay button
      if (!this.micButton) {
        this.micButton = document.createElement('ha-icon-button');
        this.micButton.style.cssText = 'position: absolute !important; top: 16px !important; left: 16px !important; z-index: 2147483647 !important; border-radius: 50% !important; color: white !important; box-shadow: 0 4px 6px rgba(0,0,0,0.3) !important; transition: all 0.2s !important; display: none !important;';
        
        const updateIcon = () => {
          this.micButton.innerHTML = `<ha-icon icon="${this.micEnabled ? 'mdi:microphone' : 'mdi:microphone-off'}"></ha-icon>`;
          this.micButton.style.backgroundColor = this.micEnabled ? 'rgba(220, 53, 69, 0.8)' : 'rgba(0, 0, 0, 0.5)';
        };
        updateIcon();

        this.micButton.addEventListener('click', () => {
          if (!this.micEnabled && !window.isSecureContext) {
            console.warn("Microphone access requires a secure connection (HTTPS). Please access Home Assistant via HTTPS.");
          }

          this.micEnabled = !this.micEnabled;
          updateIcon();
          
          if (this.micEnabled) {
            // Save current mute state and force mute to prevent echo
            this.savedMuteState = this.isMuted;
            this.isMuted = true;
          } else {
            // Restore previous mute state
            this.isMuted = this.savedMuteState !== undefined ? this.savedMuteState : this.isMuted;
          }
          
          const root = this.content?.shadowRoot || this.content;
          if (root) {
            const video = root.querySelector('video');
            const audio = root.querySelector('audio');
            const volumeIcon = root.querySelector('.volume');
            if (video) video.muted = this.isMuted;
            if (audio) audio.muted = this.isMuted;
            if (volumeIcon) volumeIcon.icon = this.isMuted ? 'mdi:volume-mute' : 'mdi:volume-high';
          }
          
          // Re-render the child to apply the new media config
          webrtcConfig.media = this.micEnabled ? 'video,audio,microphone' : 'video,audio';
          webrtcConfig.muted = this.isMuted;
          if (this.content && this.content.setConfig) {
            this.content.setConfig(webrtcConfig);
            if (this.content.nextStream) {
              this.content.nextStream(true);
            }
          }
        });
      }
      
      if (!this.bpmOverlay) {
        this.bpmOverlay = document.createElement('div');
        this.bpmOverlay.style.cssText = 'position: absolute !important; top: 16px !important; left: 50% !important; transform: translateX(-50%) !important; z-index: 2147483647 !important; color: white !important; text-shadow: 1px 1px 3px black !important; font-weight: bold !important; font-size: 14px !important; pointer-events: none !important; background: rgba(0,0,0,0.3) !important; padding: 4px 10px !important; border-radius: 12px !important; align-items: center; justify-content: center;';
        this.appendChild(this.bpmOverlay);
      }
      
      if (!this.envOverlay) {
        this.envOverlay = document.createElement('div');
        this.envOverlay.style.cssText = 'position: absolute !important; bottom: 60px !important; left: 16px !important; z-index: 2147483647 !important; color: white !important; text-shadow: 1px 1px 3px black !important; font-weight: bold !important; font-size: 14px !important; pointer-events: none !important; background: rgba(0,0,0,0.3) !important; padding: 4px 10px !important; border-radius: 12px !important; display: flex !important; gap: 10px !important; align-items: center;';
        this.appendChild(this.envOverlay);
      }


      customElements.whenDefined('webrtc-camera').then(() => {
        if (!this.content) {
          this.content = document.createElement('webrtc-camera');
          if (this.content.setConfig) {
            this.content.setConfig(webrtcConfig);
          }
          this.content.hass = this._hass;
          this.appendChild(this.content);
          if (this.bpmOverlay) this.appendChild(this.bpmOverlay);
          if (this.envOverlay) this.appendChild(this.envOverlay);
          
          
          // Add Music Player Bar & Song Library
          const defaultSongs = [];

          const loadCustomSongs = () => {
            try {
              let libraryState = null;
              if (this._hass && this._hass.states) {
                for (const key in this._hass.states) {
                  if (key.startsWith('sensor.cuboai_media_library')) {
                    libraryState = this._hass.states[key];
                    break;
                  }
                }
              }
              // One-time migration from local storage
              let migrated = localStorage.getItem(`cuboai_custom_songs_migrated_${deviceId}`);
              let stored = localStorage.getItem(`cuboai_custom_songs_${deviceId}`);
              if (!migrated && stored) {
                const parsed = JSON.parse(stored);
                if (parsed && parsed.length > 0) {
                  localStorage.setItem(`cuboai_custom_songs_migrated_${deviceId}`, 'true');
                  setTimeout(() => saveCustomSongs(parsed), 500);
                  return Array.isArray(parsed) ? parsed.filter(s => s) : [];
                }
              }

              if (libraryState && libraryState.attributes && libraryState.attributes.custom_songs) {
                return JSON.parse(JSON.stringify(libraryState.attributes.custom_songs));
              }
            } catch(e) {}
            return JSON.parse(JSON.stringify(defaultSongs));
          };

          const saveCustomSongs = (songs) => {
            localStorage.setItem(`cuboai_custom_songs_${deviceId}`, JSON.stringify(songs));
            if (this._hass) {
              this._hass.callService('cuboai', 'save_custom_songs', { songs: songs });
            }
          };

          const loadPlaylists = () => {
            try {
              let libraryState = null;
              if (this._hass && this._hass.states) {
                for (const key in this._hass.states) {
                  if (key.startsWith('sensor.cuboai_media_library')) {
                    libraryState = this._hass.states[key];
                    break;
                  }
                }
              }
              // One-time migration from local storage
              let migrated = localStorage.getItem(`cuboai_playlists_migrated_${deviceId}`);
              let stored = localStorage.getItem(`cuboai_playlists_${deviceId}`);
              if (!migrated && stored) {
                const parsed = JSON.parse(stored);
                if (parsed && parsed.length > 0) {
                  localStorage.setItem(`cuboai_playlists_migrated_${deviceId}`, 'true');
                  setTimeout(() => savePlaylists(parsed), 500);
                  return Array.isArray(parsed) ? parsed : [];
                }
              }

              if (libraryState && libraryState.attributes && libraryState.attributes.playlists) {
                return JSON.parse(JSON.stringify(libraryState.attributes.playlists));
              }
              return [];
            } catch (e) {
              return [];
            }
          };

          const savePlaylists = (playlists) => {
            localStorage.setItem(`cuboai_playlists_${deviceId}`, JSON.stringify(playlists));
            if (this._hass) {
              this._hass.callService('cuboai', 'save_playlists', { playlists: playlists });
            }
          };

          if (this._playlistPage === undefined) {
             this._playlistPage = 1;
             this._songPage = 1;
             this._shuffleMode = localStorage.getItem(`cuboai_shuffle_${deviceId}`) === 'true';
             this._repeatMode = localStorage.getItem(`cuboai_repeat_${deviceId}`) || 'off';
             
             // Only clear inPlaylist if it's the first render to prevent checking jumping
             const cs = loadCustomSongs();
             let changed = false;
             cs.forEach(s => { if (s.inPlaylist) { s.inPlaylist = false; changed = true; } });
             if (changed) saveCustomSongs(cs);
          }
          this.musicBar = document.createElement('div');
          this.musicBar.style.cssText = 'display: flex; flex-direction: column; margin-top: 8px; padding: 12px; border-top: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12)); background: var(--card-background-color, #fff); border-radius: 0 0 12px 12px; font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); color: var(--primary-text-color, #212121);';

          this.musicBar.innerHTML = `
            <style>
              .cubo-row { display: flex; align-items: center; margin-bottom: 8px; gap: 8px; }
              .cubo-input { flex-grow: 1; padding: 8px 12px; border-radius: 6px; border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.2)); background: var(--card-background-color, #fff); color: var(--primary-text-color, #000); font-size: 14px; outline: none; transition: border-color 0.2s; }
              .cubo-input:focus { border-color: var(--primary-color, #03a9f4); }
              .cubo-btn { padding: 8px 16px; border-radius: 6px; border: none; background: var(--primary-color, #03a9f4); color: white; font-weight: bold; cursor: pointer; font-size: 14px; transition: opacity 0.2s; }
              .cubo-btn:hover { opacity: 0.9; }
              .cubo-btn-red { background: var(--error-color, #f44336); }
              .cubo-btn-sec { background: var(--secondary-background-color, #e0e0e0); color: var(--primary-text-color, #212121); }
              
              .library-header { display: flex; align-items: center; justify-content: space-between; margin-top: 12px; padding-top: 8px; border-top: 1px dashed var(--divider-color, rgba(0, 0, 0, 0.1)); }
              .library-title { font-size: 14px; font-weight: bold; color: var(--secondary-text-color, #727272); }
              
              .filter-bar { display: flex; gap: 8px; margin-top: 8px; margin-bottom: 8px; }
              .cubo-select { padding: 6px; border-radius: 6px; border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.2)); background: var(--card-background-color, #fff); color: var(--primary-text-color, #000); font-size: 12px; outline: none; }
              
              .song-list { max-height: 150px; overflow-y: auto; margin-top: 8px; display: flex; flex-direction: column; gap: 4px; padding-right: 4px; }
              .song-item { display: flex; align-items: center; justify-content: space-between; padding: 6px 10px; border-radius: 6px; background: var(--secondary-background-color, #f5f5f5); border: 1px solid var(--divider-color, rgba(0,0,0,0.05)); font-size: 13px; }
              .song-info { display: flex; align-items: center; gap: 8px; flex-grow: 1; min-width: 0; }
              .song-badge { font-size: 10px; padding: 2px 6px; border-radius: 4px; background: var(--primary-color, #03a9f4); color: white; text-transform: uppercase; font-weight: bold; }
              .song-badge.spotify { background: #1ed760; }
              .song-badge.youtube { background: #ff0000; }
              .song-badge.lullabies { background: #9c27b0; }
              .song-badge.custom { background: #607d8b; }
              .song-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }
              
              .song-actions { display: flex; gap: 6px; align-items: center; }
              .icon-btn { border: none; background: none; color: var(--primary-text-color, #212121); cursor: pointer; display: flex; align-items: center; justify-content: center; padding: 4px; border-radius: 4px; transition: background 0.2s; }
              .icon-btn:hover { background: rgba(0,0,0,0.08); }
              .icon-btn.delete { color: var(--error-color, #f44336); }
              
              .add-form { display: none; flex-direction: column; gap: 8px; margin-top: 10px; padding: 10px; border-radius: 8px; background: var(--secondary-background-color, #fafafa); border: 1px solid var(--divider-color, rgba(0,0,0,0.08)); }
            </style>

            <div id="nowPlaying" class="cubo-row" style="display: none; background: rgba(3, 169, 244, 0.1); padding: 8px 12px; border-radius: 6px; border: 1px solid rgba(3, 169, 244, 0.2); margin-bottom: 12px; align-items: center;">
              <ha-icon icon="mdi:volume-high" style="color: var(--primary-color, #03a9f4); margin-right: 8px;"></ha-icon>
              <span id="nowPlayingText" style="font-weight: bold; font-size: 13px; color: var(--primary-color, #03a9f4);">Now Playing: ...</span>
            </div>

            <div class="cubo-row">
              <ha-icon icon="mdi:music" style="color: var(--secondary-text-color);"></ha-icon>
              <input type="text" id="musicUrl" class="cubo-input" placeholder="Paste YouTube or Spotify link...">
              <button id="playMusicBtn" class="cubo-btn">Play</button>
              <button id="stopMusicBtn" class="cubo-btn cubo-btn-red">Stop</button>
            </div>

            <div class="library-header" style="justify-content: space-between;">
              <div class="library-title">Saved Playlists</div>
              <div style="display: flex; gap: 6px; align-items: center;">
                <select id="playlistUserFilter" class="cubo-select" style="padding: 2px; font-size: 11px; min-height: unset; height: auto;">
                  <option value="all" ${this._playlistUserFilter === 'me' ? '' : 'selected'}>All Users</option>
                  <option value="me" ${this._playlistUserFilter === 'me' ? 'selected' : ''}>My Playlists</option>
                </select>
                <button id="toggleShuffleBtn" class="cubo-btn cubo-btn-sec" style="padding: 2px 6px; font-size: 11px; display: flex; align-items: center; gap: 4px;">
                  <ha-icon icon="mdi:shuffle" style="--mdc-icon-size: 14px;"></ha-icon> <span>Shuffle: OFF</span>
                </button>
                <button id="toggleRepeatBtn" class="cubo-btn cubo-btn-sec" style="padding: 2px 6px; font-size: 11px; display: flex; align-items: center; gap: 4px;">
                  <ha-icon icon="mdi:repeat" style="--mdc-icon-size: 14px;"></ha-icon> <span>Repeat: OFF</span>
                </button>
                <select id="playTimeSelect" class="cubo-select" style="padding: 2px; font-size: 11px; min-height: unset; height: auto; max-width: 130px;" title="Speaker Play Time">
                  <option value="0">Play Time: Infinite</option>
                  <option value="10">10 mins</option>
                  <option value="20">20 mins</option>
                  <option value="30">30 mins</option>
                  <option value="60">1 hour</option>
                  <option value="90">1.5 hours</option>
                  <option value="120">2 hours</option>
                </select>
              </div>
            </div>
            
            <div id="playlistsContainer" style="display: flex; flex-direction: column; gap: 4px; margin-top: 8px;"></div>
            
            <div class="library-header" style="margin-top: 16px;">
              <div class="library-title">Song Library</div>
              <div style="display: flex; gap: 6px;">
                <button id="toggleAddFormBtn" class="cubo-btn cubo-btn-sec" style="padding: 4px 8px; font-size: 12px;">+ Add Song</button>
              </div>
            </div>



            <div id="addForm" class="add-form">
              <div id="addFormTitle" style="font-weight: bold; font-size: 12px; margin-bottom: 4px;">Add New Song to Library</div>
              <div id="addFormError" style="color: var(--error-color, #f44336); font-size: 11px; margin-bottom: 4px; display: none;"></div>
              <input type="text" id="newSongName" class="cubo-input" placeholder="Song Name" style="margin-bottom: 4px;">
              <input type="text" id="newSongUrl" class="cubo-input" placeholder="YouTube or Spotify Link" style="margin-bottom: 4px;">
              <div class="cubo-row" style="margin-bottom: 0;">
                <select id="newSongCat" class="cubo-select" style="flex-grow: 1;">
                  <option value="youtube">YouTube</option>
                  <option value="spotify">Spotify</option>
                </select>
                <button id="saveSongBtn" class="cubo-btn" style="padding: 6px 12px; font-size: 12px;">Save</button>
              </div>
            </div>

            <div class="filter-bar" style="display: flex; gap: 8px; flex-wrap: wrap;">
              <input type="text" id="searchBar" class="cubo-input" placeholder="Search song name..." style="padding: 4px 8px; font-size: 12px; flex-grow: 1; min-width: 0;">
              <select id="categoryFilter" class="cubo-select" style="max-width: 100px;">
                <option value="all">All Categories</option>
                <option value="youtube">YouTube</option>
                <option value="spotify">Spotify</option>
                <option value="lullabies">Lullabies</option>
                <option value="user_added">User Added</option>
              </select>
              <select id="userFilter" class="cubo-select" style="max-width: 100px;">
                <option value="all">All Users</option>
              </select>
              <select id="sortFilter" class="cubo-select" style="max-width: 100px;">
                <option value="newest">Newest First</option>
                <option value="oldest">Oldest First</option>
                <option value="name_asc">Name (A-Z)</option>
                <option value="name_desc">Name (Z-A)</option>
              </select>
            </div>

            <div id="songListContainer" class="song-list"></div>

            <div id="quickAddModal" style="display: none; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 1000; align-items: center; justify-content: center; backdrop-filter: blur(2px); border-radius: 0 0 12px 12px;">
              <div style="background: var(--card-background-color, #fff); padding: 16px; border-radius: 8px; width: 85%; max-width: 300px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); display: flex; flex-direction: column;">
                <div style="font-weight: bold; margin-bottom: 12px; color: var(--primary-text-color, #212121);">Add to Playlist</div>
                <div id="quickAddPlaylistList" style="max-height: 150px; overflow-y: auto; margin-bottom: 12px; border: 1px solid var(--divider-color, rgba(0,0,0,0.1)); border-radius: 4px; display: flex; flex-direction: column;">
                </div>
                <div style="display: flex; gap: 8px; margin-bottom: 12px;">
                  <input type="text" id="quickAddNewName" class="cubo-input" placeholder="New Playlist..." style="flex-grow: 1; padding: 6px 8px; font-size: 12px;">
                  <button id="quickAddNewBtn" class="cubo-btn" style="padding: 6px 12px; font-size: 12px;">Create</button>
                </div>
                <div style="text-align: right;">
                  <button id="quickAddCancelBtn" class="cubo-btn cubo-btn-sec" style="padding: 6px 12px; font-size: 12px;">Cancel</button>
                </div>
              </div>
            </div>
          `;

          this.appendChild(this.musicBar);
          
          const playBtn = this.musicBar.querySelector('#playMusicBtn');
          const stopBtn = this.musicBar.querySelector('#stopMusicBtn');
          const inputUrl = this.musicBar.querySelector('#musicUrl');
          const searchBar = this.musicBar.querySelector('#searchBar');
          const categoryFilter = this.musicBar.querySelector('#categoryFilter');
          const userFilter = this.musicBar.querySelector('#userFilter');
          const songListContainer = this.musicBar.querySelector('#songListContainer');
          
          const toggleAddFormBtn = this.musicBar.querySelector('#toggleAddFormBtn');
          const addForm = this.musicBar.querySelector('#addForm');
          const saveSongBtn = this.musicBar.querySelector('#saveSongBtn');
          
          const playlistsContainer = this.musicBar.querySelector('#playlistsContainer');
          const savePlaylistBtn = this.musicBar.querySelector('#savePlaylistBtn');
          const savePlaylistForm = this.musicBar.querySelector('#savePlaylistForm');
          const newPlaylistName = this.musicBar.querySelector('#newPlaylistName');
          const existingPlaylistSelect = this.musicBar.querySelector('#existingPlaylistSelect');
          const confirmSavePlaylistBtn = this.musicBar.querySelector('#confirmSavePlaylistBtn');
          const cancelSavePlaylistBtn = this.musicBar.querySelector('#cancelSavePlaylistBtn');
          const toggleShuffleBtn = this.musicBar.querySelector('#toggleShuffleBtn');
          const toggleRepeatBtn = this.musicBar.querySelector('#toggleRepeatBtn');
          const playTimeSelect = this.musicBar.querySelector('#playTimeSelect');
          
          const newSongName = this.musicBar.querySelector('#newSongName');
          const newSongUrl = this.musicBar.querySelector('#newSongUrl');
          const newSongCat = this.musicBar.querySelector('#newSongCat');
          const addFormError = this.musicBar.querySelector('#addFormError');

          // Render list of songs based on search and filters
          const renderSongs = () => {
            this._renderSongsFn = renderSongs;
            try {
              const pItemsPerPage = 5;
              const sItemsPerPage = 10;
              
              const query = searchBar.value.toLowerCase();
              const filter = categoryFilter.value;
              const customSongs = loadCustomSongs().filter(s => s);
              const playlists = loadPlaylists();
              
              let filteredPlaylists = playlists;
              const selectedUserFilter = userFilter ? userFilter.value : 'all';
              
              filteredPlaylists = filteredPlaylists.filter(p => {
                const matchesSearch = p.name.toLowerCase().includes(query);
                const matchesUser = selectedUserFilter === 'all' || (p.addedBy || 'System') === selectedUserFilter;
                return matchesSearch && matchesUser;
              });
              
              const totalPlaylistPages = Math.ceil(filteredPlaylists.length / pItemsPerPage) || 1;
              if (this._playlistPage > totalPlaylistPages) this._playlistPage = totalPlaylistPages;
              
              const pStart = (this._playlistPage - 1) * pItemsPerPage;
              const paginatedPlaylists = filteredPlaylists.slice(pStart, pStart + pItemsPerPage);

              let plHtml = filteredPlaylists.length === 0 
                ? `<div style="font-size: 12px; color: var(--secondary-text-color); font-style: italic; padding: 4px;">No saved playlists yet.</div>`
                : paginatedPlaylists.map(pl => `
                  <div class="song-item" style="flex-direction: column; align-items: stretch; padding: 0; background: var(--card-background-color, #fff);">
                    <div style="display: flex; justify-content: space-between; align-items: center; padding: 10px;">
                      <div class="song-info">
                        <div class="song-name" style="font-weight: bold;">${pl.name}</div>
                        <div style="font-size: 11px; color: var(--secondary-text-color);">${pl.songs.length} songs</div>
                        <div style="font-size: 10px; color: var(--secondary-text-color, #727272); font-style: italic; margin-top: 2px;">Added by: ${pl.addedBy || 'System'}</div>
                      </div>
                      <div class="song-actions">
                        <button class="icon-btn play-playlist-btn" data-id="${pl.id}" title="Play playlist">
                          <ha-icon icon="mdi:play" style="--mdc-icon-size: 20px; color: #4caf50;"></ha-icon>
                        </button>
                        <button class="icon-btn edit-playlist-btn" data-id="${pl.id}" title="Edit playlist">
                          <ha-icon icon="mdi:pencil" style="--mdc-icon-size: 20px; color: var(--secondary-text-color, #727272);"></ha-icon>
                        </button>
                        <button class="icon-btn delete-playlist-btn" data-id="${pl.id}" title="Delete playlist">
                          <ha-icon icon="mdi:delete" style="--mdc-icon-size: 20px; color: var(--error-color, #f44336);"></ha-icon>
                        </button>
                      </div>
                    </div>
                    <div id="playlist-edit-${pl.id}" style="display: ${this._expandedPlaylist === pl.id ? 'flex' : 'none'}; flex-direction: column; gap: 4px; padding: 0 10px 10px 10px; border-top: 1px dashed var(--divider-color, rgba(0,0,0,0.05));">
                      ${pl.songs.map((sUrl, idx) => {
                        const sObj = customSongs.find(cs => cs.url === sUrl);
                        const sName = sObj ? sObj.name : 'Unknown Song';
                        return `
                          <div style="display: flex; justify-content: space-between; align-items: center; padding: 4px; font-size: 12px; border-bottom: 1px solid var(--divider-color, rgba(0,0,0,0.03));">
                            <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${sName}</span>
                            <button class="icon-btn remove-from-playlist-btn" data-plid="${pl.id}" data-idx="${idx}" style="padding: 2px;">
                              <ha-icon icon="mdi:close" style="--mdc-icon-size: 16px; color: var(--error-color, #f44336);"></ha-icon>
                            </button>
                          </div>
                        `;
                      }).join('')}
                    </div>
                  </div>
                `).join('');
                
              if (toggleShuffleBtn) {
                toggleShuffleBtn.innerHTML = `<ha-icon icon="mdi:shuffle" style="--mdc-icon-size: 14px; color: ${this._shuffleMode ? '#4caf50' : 'inherit'};"></ha-icon> <span>Shuffle: ${this._shuffleMode ? 'ON' : 'OFF'}</span>`;
              }
              if (toggleRepeatBtn) {
                let rColor = this._repeatMode === 'off' ? 'inherit' : '#4caf50';
                let rIcon = this._repeatMode === 'one' ? 'mdi:repeat-once' : 'mdi:repeat';
                toggleRepeatBtn.innerHTML = `<ha-icon icon="${rIcon}" style="--mdc-icon-size: 14px; color: ${rColor};"></ha-icon> <span>Repeat: ${this._repeatMode.toUpperCase()}</span>`;
              }
              
              if (existingPlaylistSelect) {
                const currentVal = existingPlaylistSelect.value;
                existingPlaylistSelect.innerHTML = '<option value="">-- Create New Playlist --</option>' + playlists.map(p => `<option value="${p.id}">${p.name}</option>`).join('');
                if (playlists.some(p => p.id === currentVal)) existingPlaylistSelect.value = currentVal;
              }

              if (playlists.length > pItemsPerPage) {
                plHtml += `
                  <div style="display: flex; justify-content: space-between; align-items: center; padding: 4px; font-size: 12px;">
                    <button class="cubo-btn cubo-btn-sec playlist-prev-btn" style="padding: 2px 6px; ${this._playlistPage === 1 ? 'opacity: 0.5; pointer-events: none;' : ''}">Prev</button>
                    <span>Page ${this._playlistPage} of ${totalPlaylistPages}</span>
                    <button class="cubo-btn cubo-btn-sec playlist-next-btn" style="padding: 2px 6px; ${this._playlistPage === totalPlaylistPages ? 'opacity: 0.5; pointer-events: none;' : ''}">Next</button>
                  </div>
                `;
              }
              playlistsContainer.innerHTML = plHtml;
            
            // Populate User Filter select dropdown dynamically
            const prevSelectedUser = userFilter.value;
            const songUsers = customSongs.map(s => s.addedBy || 'System');
            const playlistUsers = playlists.map(p => p.addedBy || 'System');
            const users = [...new Set([...songUsers, ...playlistUsers])];
            userFilter.innerHTML = '<option value="all">All Users</option>' + users.map(u => `<option value="${u}">${u}</option>`).join('');
            if (users.includes(prevSelectedUser)) {
              userFilter.value = prevSelectedUser;
            } else {
              userFilter.value = 'all';
            }

            // Get dynamic sources from the lullaby media player
            const lullabyState = this._hass && this._lullabyEntityId ? this._hass.states[this._lullabyEntityId] : null;
            const sources = (lullabyState && lullabyState.attributes && lullabyState.attributes.source_list) || [];
            const lullabySongs = sources.map(sourceName => ({
              name: sourceName,
              url: sourceName,
              category: "Lullabies",
              custom: false,
              isLullaby: true
            }));

            // Fallback to default if offline/not loaded
            const actualLullabies = lullabySongs.length > 0 ? lullabySongs : [
              { name: "Camera Lullaby", url: "CuboAI_Lullaby", category: "Lullabies", custom: false, isLullaby: true }
            ];

            // Filter custom songs based on search
            const filteredCustom = customSongs.map((song, index) => ({...song, _originalIndex: index})).filter(song => {
              const matchesSearch = song.name.toLowerCase().includes(query) || song.url.toLowerCase().includes(query);
              let matchesCategory = true;
              if (filter === "youtube") matchesCategory = song.category.toLowerCase() === "youtube";
              else if (filter === "spotify") matchesCategory = song.category.toLowerCase() === "spotify";
              else if (filter === "lullabies") matchesCategory = false; // Lullabies are handled separately below
              else if (filter === "user_added") matchesCategory = song.custom === true;
              
              const selectedUserFilter = userFilter.value;
              const matchesUser = selectedUserFilter === 'all' || (song.addedBy || 'System') === selectedUserFilter;
              
              return matchesSearch && matchesCategory && matchesUser;
            });
            
            const sortFilter = this.musicBar.querySelector('#sortFilter');
            const sortVal = sortFilter ? sortFilter.value : 'newest';
            if (sortVal === 'name_asc') {
              filteredCustom.sort((a, b) => a.name.localeCompare(b.name));
            } else if (sortVal === 'name_desc') {
              filteredCustom.sort((a, b) => b.name.localeCompare(a.name));
            } else if (sortVal === 'oldest') {
              filteredCustom.sort((a, b) => a._originalIndex - b._originalIndex);
            } else {
              // newest
              filteredCustom.sort((a, b) => b._originalIndex - a._originalIndex);
            }

            this._filteredSongs = filteredCustom;

            // Generate HTML for Expandable Lullabies
            let lullabiesHtml = '';
            if (filter === "all" || filter === "lullabies") {
              const matchesLullabySearch = "camera lullabies".includes(query) || "lullabies".includes(query) || actualLullabies.some(s => s.name.toLowerCase().includes(query));
              if (matchesLullabySearch) {
                lullabiesHtml = `
                  <div class="song-item" style="flex-direction: column; align-items: stretch; gap: 0; padding: 0;">
                    <div id="toggleLullabiesBtn" class="song-info" style="cursor: pointer; padding: 10px; display: flex; align-items: center; justify-content: space-between; user-select: none;">
                      <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="song-badge lullabies">Lullabies</span>
                        <div class="song-name" style="font-weight: bold;">Camera Lullabies</div>
                      </div>
                      <ha-icon id="lullabyChevron" icon="${this._lullabiesExpanded ? 'mdi:chevron-up' : 'mdi:chevron-down'}" style="color: var(--secondary-text-color);"></ha-icon>
                    </div>
                    <div id="lullabiesSublist" style="display: ${this._lullabiesExpanded ? 'flex' : 'none'}; flex-direction: column; gap: 4px; padding: 0 10px 10px 10px; border-top: 1px dashed var(--divider-color, rgba(0,0,0,0.05));">
                      ${actualLullabies.map((song) => `
                        <div class="song-item" style="background: var(--card-background-color, #fff); margin-top: 4px; padding: 6px 10px; border: 1px solid var(--divider-color, rgba(0,0,0,0.03));">
                          <div class="song-info">
                            <div class="song-name" style="font-size: 13px;">${song.name}</div>
                          </div>
                          <div class="song-actions">
                            ${this._expandedPlaylist ? `
                              <button class="cubo-btn add-to-active-btn" data-url="${song.url}" style="padding: 2px 8px; font-size: 11px; background: var(--success-color, #28a745);">Add</button>
                            ` : `
                              <button class="icon-btn quick-add-btn" data-url="${song.url}" title="Add to Playlist">
                                <ha-icon icon="mdi:playlist-plus" style="--mdc-icon-size: 20px; color: var(--secondary-text-color, #727272);"></ha-icon>
                              </button>
                            `}
                            <button class="icon-btn play-song-btn" data-url="${song.url}" data-lullaby="true" title="Play song">
                              <ha-icon icon="mdi:play" style="--mdc-icon-size: 20px; color: var(--primary-color, #03a9f4);"></ha-icon>
                            </button>
                          </div>
                        </div>
                      `).join('')}
                    </div>
                  </div>
                `;
              }
            }

            // Render Custom/Added list of songs
            const totalSongPages = Math.ceil(filteredCustom.length / sItemsPerPage) || 1;
            if (this._songPage > totalSongPages) this._songPage = totalSongPages;
            const sStart = (this._songPage - 1) * sItemsPerPage;
            const paginatedCustom = filteredCustom.slice(sStart, sStart + sItemsPerPage);

            let customSongsHtml = paginatedCustom.map((song) => {
              const originalIdx = customSongs.findIndex(s => s.name === song.name && s.url === song.url);
              return `
                <div class="song-item">
                  <div class="song-info" style="align-items: center; gap: 8px;">
                    
                    <span class="song-badge ${song.category.toLowerCase()}">${song.category}</span>
                    <div style="display: flex; flex-direction: column; min-width: 0;">
                      <div class="song-name" title="${song.name}" style="font-weight: 500;">${song.name}</div>
                      <div class="song-url" title="${song.url}" style="font-size: 11px; color: var(--secondary-text-color, #727272); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 250px;">${song.url}</div>
                      <div class="song-added-by" style="font-size: 10px; color: var(--secondary-text-color, #727272); font-style: italic;">Added by: ${song.addedBy || 'System'}</div>
                    </div>
                  </div>
                  <div class="song-actions">
                    ${this._expandedPlaylist ? `
                      <button class="cubo-btn add-to-active-btn" data-url="${song.url}" style="padding: 2px 8px; font-size: 11px; margin-right: 4px; background: var(--success-color, #28a745);">Add</button>
                    ` : `
                      <button class="icon-btn quick-add-btn" data-url="${song.url}" title="Add to Playlist">
                        <ha-icon icon="mdi:playlist-plus" style="--mdc-icon-size: 20px; color: var(--secondary-text-color, #727272);"></ha-icon>
                      </button>
                    `}
                    <button class="icon-btn play-song-btn" data-url="${song.url}" data-lullaby="false" title="Play song">
                      <ha-icon icon="mdi:play" style="--mdc-icon-size: 20px; color: var(--primary-color, #03a9f4);"></ha-icon>
                    </button>
                    <button class="icon-btn edit-song-btn" data-index="${song._originalIndex}" title="Edit song">
                      <ha-icon icon="mdi:pencil" style="--mdc-icon-size: 20px; color: var(--secondary-text-color, #727272);"></ha-icon>
                    </button>
                    <button class="icon-btn delete-song-btn" data-index="${song._originalIndex}" title="Delete song">
                      <ha-icon icon="mdi:delete" style="--mdc-icon-size: 20px; color: var(--error-color, #f44336);"></ha-icon>
                    </button>
                  </div>
                </div>
              `;
            }).join('');

            if (filteredCustom.length > sItemsPerPage) {
              customSongsHtml += `
                <div style="display: flex; justify-content: space-between; align-items: center; padding: 8px 4px; font-size: 12px;">
                  <button class="cubo-btn cubo-btn-sec song-prev-btn" style="padding: 2px 8px; ${this._songPage === 1 ? 'opacity: 0.5; pointer-events: none;' : ''}">Prev</button>
                  <span>Page ${this._songPage} of ${totalSongPages}</span>
                  <button class="cubo-btn cubo-btn-sec song-next-btn" style="padding: 2px 8px; ${this._songPage === totalSongPages ? 'opacity: 0.5; pointer-events: none;' : ''}">Next</button>
                </div>
              `;
            }

            songListContainer.innerHTML = lullabiesHtml + customSongsHtml;

            // Bind Lullabies accordion click
            const toggleLullabiesBtn = songListContainer.querySelector('#toggleLullabiesBtn');
            if (toggleLullabiesBtn) {
              toggleLullabiesBtn.addEventListener('click', () => {
                this._lullabiesExpanded = !this._lullabiesExpanded;
                renderSongs();
              });
            }

            // Bind Quick Add and Add to Active
            songListContainer.querySelectorAll('.add-to-active-btn').forEach(btn => {
              btn.addEventListener('click', () => {
                const url = btn.getAttribute('data-url');
                let playlists = loadPlaylists();
                const pl = playlists.find(p => p.id === this._expandedPlaylist);
                if (pl) {
                  pl.songs.push(url);
                  savePlaylists(playlists);
                }
              });
            });

            songListContainer.querySelectorAll('.quick-add-btn').forEach(btn => {
              btn.addEventListener('click', () => {
                const url = btn.getAttribute('data-url');
                this._quickAddUrl = url;
                
                const playlists = loadPlaylists();
                const listContainer = this.musicBar.querySelector('#quickAddPlaylistList');
                if (listContainer) {
                  listContainer.innerHTML = playlists.length === 0 
                    ? '<div style="padding: 8px; font-size: 11px; font-style: italic; color: #999; text-align: center;">No playlists created yet.</div>'
                    : playlists.map(p => `<div class="quick-add-pl-option" data-id="${p.id}" style="padding: 8px 12px; font-size: 13px; cursor: pointer; border-bottom: 1px solid var(--divider-color, rgba(0,0,0,0.05));">${p.name}</div>`).join('');
                    
                  // Bind clicks to options
                  listContainer.querySelectorAll('.quick-add-pl-option').forEach(opt => {
                    opt.addEventListener('click', () => {
                      const plId = opt.getAttribute('data-id');
                      let pls = loadPlaylists();
                      const pl = pls.find(p => p.id === plId);
                      if (pl) {
                        pl.songs.push(this._quickAddUrl);
                        savePlaylists(pls);
                      }
                      this.musicBar.querySelector('#quickAddModal').style.display = 'none';
                    });
                  });
                }
                
                this.musicBar.querySelector('#quickAddModal').style.display = 'flex';
                this.musicBar.querySelector('#quickAddNewName').value = '';
              });
            });

            const quickAddModal = this.musicBar.querySelector('#quickAddModal');
            if (quickAddModal && !quickAddModal.dataset.bound) {
              quickAddModal.dataset.bound = "true";
              
              this.musicBar.querySelector('#quickAddCancelBtn').addEventListener('click', () => {
                quickAddModal.style.display = 'none';
              });
              
              this.musicBar.querySelector('#quickAddNewBtn').addEventListener('click', () => {
                const name = this.musicBar.querySelector('#quickAddNewName').value.trim();
                if (name && this._quickAddUrl) {
                  let pls = loadPlaylists();
                  pls.push({ id: Date.now().toString(), name: name, songs: [this._quickAddUrl], addedBy: this._hass && this._hass.user ? this._hass.user.name : "System" });
                  savePlaylists(pls);
                  quickAddModal.style.display = 'none';
                }
              });
            }

            // Bind play button listeners
            songListContainer.querySelectorAll('.play-song-btn').forEach(btn => {
              btn.addEventListener('click', (e) => {
                const url = btn.getAttribute('data-url');
                const type = btn.getAttribute('data-type');
                const name = btn.getAttribute('data-name');
                
                if (this._hass) {
                    if (type === 'lullaby') {
                        if (this._lullabyEntityId) {
                            this._hass.callService('media_player', 'select_source', {
                                entity_id: this._lullabyEntityId,
                                source: name
                            });
                        }
                    } else {
                        this._hass.callService('media_player', 'play_media', {
                          entity_id: this._speakerEntityId,
                          media_content_id: url,
                          media_content_type: 'music',
                          enqueue: 'replace'
                        });
                    }
                }
              });
            });



            // Bind edit button listeners
            songListContainer.querySelectorAll('.edit-song-btn').forEach(btn => {
              btn.addEventListener('click', (e) => {
                const idx = parseInt(btn.getAttribute('data-index'));
                const customSongs = loadCustomSongs();
                const song = customSongs[idx];
                if (song) {
                  this._editingIndex = idx;
                  addForm.style.display = 'flex';
                  toggleAddFormBtn.textContent = 'Close';
                  
                  newSongName.value = song.name;
                  newSongUrl.value = song.url;
                  
                  const reverseCatMap = {
                    "YouTube": "youtube",
                    "Spotify": "spotify",
                    "Lullabies": "lullabies"
                  };
                  newSongCat.value = reverseCatMap[song.category] || "youtube";
                  
                  const addFormTitle = this.musicBar.querySelector('#addFormTitle');
                  if (addFormTitle) addFormTitle.textContent = "Edit Song in Library";
                  saveSongBtn.textContent = "Save Changes";
                }
              });
            });

            // Bind delete button listeners
            songListContainer.querySelectorAll('.delete-song-btn').forEach(btn => {
              btn.addEventListener('click', (e) => {
                const idx = parseInt(btn.getAttribute('data-index'));
                let customSongs = loadCustomSongs();
                if (idx >= 0 && idx < customSongs.length) {
                  customSongs.splice(idx, 1);
                  saveCustomSongs(customSongs);
                }
              });
            });

            // Bind playlist action buttons
            playlistsContainer.querySelectorAll('.play-playlist-btn').forEach(btn => {
              btn.addEventListener('click', () => {
                const id = btn.getAttribute('data-id');
                const playlists = loadPlaylists();
                const pl = playlists.find(p => p.id === id);
                if (pl && pl.songs.length > 0) {
                  let songs = [...pl.songs];
                  if (this._shuffleMode) {
                    songs = songs.sort(() => Math.random() - 0.5);
                  }
                  
                  if (this._hass) {
                    songs.forEach((url, index) => {
                      this._hass.callService('media_player', 'play_media', {
                        entity_id: this._speakerEntityId,
                        media_content_id: url,
                        media_content_type: 'music',
                        enqueue: index === 0 ? 'replace' : 'add'
                      });
                    });
                  }
                }
              });
            });

            playlistsContainer.querySelectorAll('.delete-playlist-btn').forEach(btn => {
              btn.addEventListener('click', () => {
                const id = btn.getAttribute('data-id');
                let playlists = loadPlaylists();
                playlists = playlists.filter(p => p.id !== id);
                savePlaylists(playlists);
              });
            });

            playlistsContainer.querySelectorAll('.edit-playlist-btn').forEach(btn => {
              btn.addEventListener('click', () => {
                const id = btn.getAttribute('data-id');
                this._expandedPlaylist = this._expandedPlaylist === id ? null : id;
                renderSongs();
              });
            });

            playlistsContainer.querySelectorAll('.remove-from-playlist-btn').forEach(btn => {
              btn.addEventListener('click', () => {
                const id = btn.getAttribute('data-plid');
                const idx = parseInt(btn.getAttribute('data-idx'));
                let playlists = loadPlaylists();
                const pl = playlists.find(p => p.id === id);
                if (pl) {
                  pl.songs.splice(idx, 1);
                  savePlaylists(playlists);
                }
              });
            });

            const pPrevBtn = playlistsContainer.querySelector('.playlist-prev-btn');
            if (pPrevBtn) pPrevBtn.addEventListener('click', () => { this._playlistPage = Math.max(1, this._playlistPage - 1); renderSongs(); });
            
            const pNextBtn = playlistsContainer.querySelector('.playlist-next-btn');
            if (pNextBtn) pNextBtn.addEventListener('click', () => { this._playlistPage++; renderSongs(); });

            const sPrevBtn = songListContainer.querySelector('.song-prev-btn');
            if (sPrevBtn) sPrevBtn.addEventListener('click', () => { this._songPage = Math.max(1, this._songPage - 1); renderSongs(); });
            
            const sNextBtn = songListContainer.querySelector('.song-next-btn');
            if (sNextBtn) sNextBtn.addEventListener('click', () => { this._songPage++; renderSongs(); });
            } catch (renderErr) {
              console.error(renderErr);
              songListContainer.innerHTML = `<div style="color: red; padding: 10px;">Error rendering songs: ${renderErr.message}</div>`;
            }
          };

          playBtn.addEventListener('click', () => {
             const url = inputUrl.value;
             if (url && this._hass) {
                this._hass.callService('media_player', 'play_media', {
                    entity_id: this._speakerEntityId,
                    media_content_id: url,
                    media_content_type: 'music'
                });
                inputUrl.value = '';
             }
          });
          
          stopBtn.addEventListener('click', () => {
             if (this._hass) {
                 this._hass.callService('media_player', 'media_stop', {
                     entity_id: this._speakerEntityId
                 });
                 if (this._lullabyEntityId) {
                     this._hass.callService('media_player', 'media_stop', {
                         entity_id: this._lullabyEntityId
                     });
                 }
             }
          });

          toggleAddFormBtn.addEventListener('click', () => {
            const isVisible = addForm.style.display === 'flex';
            addForm.style.display = isVisible ? 'none' : 'flex';
            toggleAddFormBtn.textContent = isVisible ? '+ Add Song' : 'Close';
            if (isVisible) {
              this._editingIndex = null;
              newSongName.value = '';
              newSongUrl.value = '';
              const addFormTitle = this.musicBar.querySelector('#addFormTitle');
              if (addFormTitle) addFormTitle.textContent = "Add Song to Library";
              saveSongBtn.textContent = "Add Song";
            }
            if (addFormError) addFormError.style.display = 'none';
          });

          // Removed playAllBtn listener

          saveSongBtn.addEventListener('click', () => {
            const name = newSongName.value.trim();
            const url = newSongUrl.value.trim();
            const cat = newSongCat.value;
            
            if (!name || !url) return;

            if (cat === "youtube" && !url.includes("youtube.com") && !url.includes("youtu.be")) {
              if (addFormError) {
                addFormError.textContent = "Please enter a valid YouTube URL (e.g. containing youtube.com or youtu.be).";
                addFormError.style.display = 'block';
              }
              return;
            }
            
            if (cat === "spotify" && !url.includes("spotify.com")) {
              if (addFormError) {
                addFormError.textContent = "Please enter a valid Spotify URL (e.g. containing spotify.com).";
                addFormError.style.display = 'block';
              }
              return;
            }

            if (addFormError) addFormError.style.display = 'none';

            const categoryMap = {
              youtube: "YouTube",
              spotify: "Spotify",
              lullabies: "Lullabies"
            };

            let customSongs = loadCustomSongs();

            if (this._editingIndex !== undefined && this._editingIndex !== null) {
              if (customSongs[this._editingIndex]) {
                customSongs[this._editingIndex].name = name;
                customSongs[this._editingIndex].url = url;
                customSongs[this._editingIndex].category = categoryMap[cat] || "YouTube";
              }
              this._editingIndex = null;
            } else {
              customSongs.push({
                name: name,
                url: url,
                category: categoryMap[cat] || "YouTube",
                custom: true,
                inPlaylist: false,
                addedBy: this._hass && this._hass.user ? this._hass.user.name : "System"
              });
            }
            
            saveCustomSongs(customSongs);

            newSongName.value = '';
            newSongUrl.value = '';
            addForm.style.display = 'none';
            toggleAddFormBtn.textContent = '+ Add Song';
            
            const addFormTitle = this.musicBar.querySelector('#addFormTitle');
            if (addFormTitle) addFormTitle.textContent = "Add New Song to Library";
            saveSongBtn.textContent = "Save";

            renderSongs();
          });
          // Removed savePlaylistBtn, cancelSavePlaylistBtn, confirmSavePlaylistBtn listeners
          
          if (toggleShuffleBtn) {
            toggleShuffleBtn.addEventListener('click', () => {
              this._shuffleMode = !this._shuffleMode;
              localStorage.setItem(`cuboai_shuffle_${deviceId}`, this._shuffleMode);
              renderSongs();
            });
          }
          
          if (toggleRepeatBtn) {
            toggleRepeatBtn.addEventListener('click', () => {
              let newMode = 'off';
              if (this._repeatMode === 'off') newMode = 'all';
              else if (this._repeatMode === 'all') newMode = 'one';
              else newMode = 'off';
              
              if (this._hass && this._speakerEntityId) {
                this._hass.callService('media_player', 'repeat_set', {
                  entity_id: this._speakerEntityId,
                  repeat: newMode
                });
              }
            });
          }
          
          if (playTimeSelect) {
            playTimeSelect.addEventListener('change', (e) => {
              if (this._hass) {
                this._hass.callService('number', 'set_value', {
                  entity_id: 'number.cuboai_speaker_timer_' + deviceId,
                  value: parseInt(e.target.value)
                });
              }
            });
          }
          

          const onFilterChange = () => {
            this._songPage = 1;
            renderSongs();
          };
          const sortFilter = this.musicBar.querySelector('#sortFilter');
          searchBar.addEventListener('input', onFilterChange);
          categoryFilter.addEventListener('change', onFilterChange);
          sortFilter.addEventListener('change', onFilterChange);
          userFilter.addEventListener('change', (e) => {
            this._userFilter = e.target.value;
            onFilterChange();
          });
          
          const playlistUserFilter = this.musicBar.querySelector('#playlistUserFilter');
          if (playlistUserFilter) {
            playlistUserFilter.addEventListener('change', (e) => {
              this._playlistUserFilter = e.target.value;
              this._playlistPage = 1;
              renderSongs();
            });
          }

          // Initial Render
          renderSongs();
        }
        
        // Use an interval to ensure the button stays attached even if the child re-renders
        if (!this.attachInterval) {
          this.attachInterval = setInterval(() => {
            // Penetrate Shadow DOM if it exists
            const root = this.content.shadowRoot || this.content;
            const player = root.querySelector('.player') || root.querySelector('.card') || root;
            const video = root.querySelector('video');
            const audio = root.querySelector('audio');
            const volumeIcon = root.querySelector('.volume');

            if (!this.micButton.isConnected || (player && !player.contains(this.micButton))) {
              if (player) player.appendChild(this.micButton);
              else root.appendChild(this.micButton);
            }
            
            if (this.bpmOverlay && (!this.bpmOverlay.isConnected || (player && !player.contains(this.bpmOverlay)))) {
              if (player) player.appendChild(this.bpmOverlay);
              else root.appendChild(this.bpmOverlay);
            }
            
            if (this.envOverlay && (!this.envOverlay.isConnected || (player && !player.contains(this.envOverlay)))) {
              if (player) player.appendChild(this.envOverlay);
              else root.appendChild(this.envOverlay);
            }
            
            if ((video || audio) && volumeIcon) {
              // Ensure the media matches our memory when it first loads
              if (video && !video.dataset.cuboInit) {
                video.dataset.cuboInit = "true";

                // Apple devices (iOS/Safari) use strict native media players and break if patched.
                // We reliably detect Apple engines by checking the vendor string.
                const isAppleWebKit = navigator.vendor && navigator.vendor.includes('Apple');

                // Fullscreen Patch: redirect video fullscreen to the player container
                if (!isAppleWebKit) {
                    const originalFs = video.requestFullscreen || video.webkitRequestFullscreen;
                    if (originalFs) {
                       video.requestFullscreen = function(options) {
                          if (player && player.requestFullscreen) return player.requestFullscreen(options);
                          if (player && player.webkitRequestFullscreen) return player.webkitRequestFullscreen(options);
                          return originalFs.call(video, options);
                       };
                       if (video.webkitRequestFullscreen) video.webkitRequestFullscreen = video.requestFullscreen;
                    }
                }


                // PiP Patch: Canvas stream overlay technique
                if (!isAppleWebKit) {
                    const originalPip = video.requestPictureInPicture;
                    if (originalPip) {
                       video.crossOrigin = "anonymous";
                   const self = this;
                   
                   const setupPip = () => {
                       if (video._pipVideo) return;
                       
                       const cvs = document.createElement('canvas');
                       cvs.width = video.videoWidth || 1920;
                       cvs.height = video.videoHeight || 1080;
                       const ctx = cvs.getContext('2d');
                       
                       const pipVideo = document.createElement('video');
                       pipVideo.muted = true;
                       pipVideo.autoplay = true;
                       
                       const stream = cvs.captureStream(30);
                       pipVideo.srcObject = stream;
                       pipVideo.style.position = 'absolute';
                       pipVideo.style.width = '1px';
                       pipVideo.style.height = '1px';
                       pipVideo.style.opacity = '0.01';
                       pipVideo.style.pointerEvents = 'none';
                       self.appendChild(pipVideo);
                       
                       pipVideo.addEventListener('volumechange', () => {
                           if (audio) {
                               audio.muted = pipVideo.muted;
                               self.isMuted = audio.muted;
                               if (volumeIcon) {
                                   volumeIcon.icon = self.isMuted ? 'mdi:volume-mute' : 'mdi:volume-high';
                               }
                           }
                       });
                       
                       video._pipVideo = pipVideo;
                       video._pipCanvas = cvs;
                       video._pipCtx = ctx;
                       
                       // Keep the canvas stream alive in the background at 1fps
                       // This ensures pipVideo successfully loads its metadata
                       // so requestPictureInPicture doesn't reject synchronously.
                       setInterval(() => {
                           if (!video._pipActive && video.videoWidth > 0) {
                               if (cvs.width !== video.videoWidth || cvs.height !== video.videoHeight) {
                                   cvs.width = video.videoWidth;
                                   cvs.height = video.videoHeight;
                               }
                               ctx.drawImage(video, 0, 0, cvs.width, cvs.height);
                           }
                       }, 1000);
                       
                       pipVideo.addEventListener('leavepictureinpicture', () => {
                          video._pipActive = false;
                       });
                       
                       pipVideo.play().catch(e => console.error("PiP background play failed", e));
                   };
                   
                   setupPip();
                   video.addEventListener('playing', setupPip);
                   if (audio) audio.addEventListener('playing', setupPip);
                   
                   video.requestPictureInPicture = function() {
                      if (!video._pipVideo) {
                          setupPip();
                      }
                      
                      // Ensure audio track is attached dynamically
                      if (audio && audio.srcObject && video._pipVideo.srcObject) {
                          const audioTracks = audio.srcObject.getAudioTracks();
                          const existingTracks = video._pipVideo.srcObject.getAudioTracks();
                          if (audioTracks.length > 0 && existingTracks.length === 0) {
                              video._pipVideo.srcObject.addTrack(audioTracks[0]);
                          }
                      }
                      
                      video._pipActive = true;
                      
                      const drawFrame = () => {
                          if (!video._pipActive) return;
                          const cvs = video._pipCanvas;
                          const ctx = video._pipCtx;
                          if (video.videoWidth > 0 && video.videoHeight > 0) {
                              if (cvs.width !== video.videoWidth || cvs.height !== video.videoHeight) {
                                  cvs.width = video.videoWidth;
                                  cvs.height = video.videoHeight;
                              }
                              ctx.drawImage(video, 0, 0, cvs.width, cvs.height);
                              
                              // Draw Overlays
                              const drawIcon = (pathData, color, x, y, size) => {
                                  ctx.save();
                                  ctx.translate(x, y);
                                  const scale = size / 24;
                                  ctx.scale(scale, scale);
                                  ctx.fillStyle = color;
                                  ctx.fill(new Path2D(pathData));
                                  ctx.restore();
                              };

                              const drawPill = (text, iconPath, iconColor, x, y, size) => {
                                  ctx.font = `bold ${size}px Arial`;
                                  const textWidth = ctx.measureText(text).width;
                                  const padding = size * 0.5;
                                  const iconSize = size * 1.2;
                                  const gap = size * 0.3;
                                  const width = padding + iconSize + gap + textWidth + padding;
                                  const height = size * 1.8;
                                  const radius = height / 2;
                                  
                                  // Draw background
                                  ctx.fillStyle = "rgba(0, 0, 0, 0.6)";
                                  ctx.beginPath();
                                  ctx.roundRect(x, y - height/2, width, height, radius);
                                  ctx.fill();
                                  
                                  // Draw icon
                                  drawIcon(iconPath, iconColor, x + padding, y - iconSize/2, iconSize);
                                  
                                  // Draw text
                                  ctx.fillStyle = "white";
                                  ctx.textAlign = "left";
                                  ctx.textBaseline = "middle";
                                  ctx.fillText(text, x + padding + iconSize + gap, y);
                                  
                                  return width;
                              };
                              
                              const pathHeartPulse = "M7.5,4A5.5,5.5 0 0,0 2,9.5C2,10 2.09,10.5 2.22,11H6.3L7.57,7.63C7.87,6.83 9.05,6.75 9.43,7.63L11.5,13L12.09,11.58C12.22,11.25 12.57,11 13,11H21.78C21.91,10.5 22,10 22,9.5A5.5,5.5 0 0,0 16.5,4C14.64,4 13,4.93 12,6.34C11,4.93 9.36,4 7.5,4V4M3,12.5A1,1 0 0,0 2,13.5A1,1 0 0,0 3,14.5H5.44L11,20C12,20.9 12,20.9 13,20L18.56,14.5H21A1,1 0 0,0 22,13.5A1,1 0 0,0 21,12.5H13.4L12.47,14.8C12.07,15.81 10.92,15.67 10.55,14.83L8.5,9.5L7.54,11.83C7.39,12.21 7.05,12.5 6.6,12.5H3Z";
                              const pathThermometer = "M15 13V5A3 3 0 0 0 9 5V13A5 5 0 1 0 15 13M12 4A1 1 0 0 1 13 5V8H11V5A1 1 0 0 1 12 4Z";
                              const pathWaterPercent = "M12,3.25C12,3.25 6,10 6,14C6,17.32 8.69,20 12,20A6,6 0 0,0 18,14C18,10 12,3.25 12,3.25M14.47,9.97L15.53,11.03L9.53,17.03L8.47,15.97M9.75,10A1.25,1.25 0 0,1 11,11.25A1.25,1.25 0 0,1 9.75,12.5A1.25,1.25 0 0,1 8.5,11.25A1.25,1.25 0 0,1 9.75,10M14.25,14.5A1.25,1.25 0 0,1 15.5,15.75A1.25,1.25 0 0,1 14.25,17A1.25,1.25 0 0,1 13,15.75A1.25,1.25 0 0,1 14.25,14.5Z";
                              
                              const bpm = self._currentBpmText;
                              if (bpm) {
                                  const textWidth = ctx.measureText(bpm).width;
                                  const size = 48;
                                  const totalWidth = size * 0.5 + size * 1.2 + size * 0.3 + textWidth + size * 0.5;
                                  drawPill(bpm, pathHeartPulse, "#f44336", cvs.width / 2 - totalWidth / 2, 80, size);
                              }
                              
                              const temp = self._currentTempText;
                              const humi = self._currentHumiText;
                              if (temp || humi) {
                                  let currentX = 40;
                                  const ey = cvs.height - 60;
                                  const gap = 20;
                                  
                                  if (temp) {
                                      currentX += drawPill(temp, pathThermometer, "#ff9800", currentX, ey, 48) + gap;
                                  }
                                  if (humi) {
                                      drawPill(humi, pathWaterPercent, "#03a9f4", currentX, ey, 48);
                                  }
                              }
                          }
                          
                          requestAnimationFrame(drawFrame);
                      };
                      
                      drawFrame();
                      
                      // Synchronously request PiP to preserve user gesture
                      return video._pipVideo.requestPictureInPicture().catch(e => {
                          video._pipActive = false;
                          console.error("Custom PiP failed, falling back to standard PiP:", e);
                          return originalPip.call(video);
                      });
                   };
                }

              }
            } // ADDED MISSING BRACKET FOR if (video && !video.dataset.cuboInit)
            
            if (audio && !audio.dataset.cuboInit) {
              audio.dataset.cuboInit = "true";
              }
              if (!volumeIcon.dataset.cuboInit) {
                volumeIcon.dataset.cuboInit = "true";
                volumeIcon.icon = this.isMuted ? 'mdi:volume-mute' : 'mdi:volume-high';
              }
              
              if (!volumeIcon.dataset.cuboHooked) {
                volumeIcon.dataset.cuboHooked = "true";
                volumeIcon.addEventListener('click', () => {
                  setTimeout(() => {
                    this.isMuted = video ? video.muted : (audio ? audio.muted : false);
                    if (audio) audio.muted = this.isMuted;
                    localStorage.setItem(`cuboai_muted_${deviceId}`, this.isMuted ? 'true' : 'false');
                  }, 100);
                });
              }
            }
          }, 500);
        }
      });
    } else {
      // If content already exists, just update hass
      if (this.content.setConfig && this._hass) {
        this.content.hass = this._hass;
      }
    }

      let tempState = null;
      let humiState = null;
      let bpmState = null;
      let babyName = null;
      
      if (this._speakerEntityId) {
          const nameParts = this._speakerEntityId.replace('media_player.', '').replace('_speaker', '').split('_');
          babyName = nameParts[nameParts.length - 1]; // e.g. "suwon"
      }

      for (const entity_id in hass.states) {
          if (entity_id.startsWith('sensor.cuboai_') && !entity_id.includes('alert')) {
              if (babyName && !entity_id.includes(babyName)) continue;
              if (entity_id.includes('temperature') && !entity_id.includes('thermometer')) tempState = hass.states[entity_id];
              else if (entity_id.includes('humidity')) humiState = hass.states[entity_id];
              else if (entity_id.includes('mat_bpm')) bpmState = hass.states[entity_id];
          }
      }

      if (this.bpmOverlay) {
        let bpmText = "??";
        if (bpmState && bpmState.state !== 'unknown' && bpmState.state !== 'unavailable') {
            const parsed = parseFloat(bpmState.state);
            bpmText = !isNaN(parsed) ? Math.round(parsed) : bpmState.state;
        }
        this.bpmOverlay.innerHTML = `<ha-icon icon="mdi:heart-pulse" style="margin-right: 4px; color: #ff4a4a; --mdc-icon-size: 18px;"></ha-icon>${bpmText} BPM`;
        this.bpmOverlay.style.display = 'flex';
        this._currentBpmText = bpmText !== "??" ? bpmText + " BPM" : "";
      }

      if (this.envOverlay) {
        let envHtml = '';
        let tempText = "??";
        let tempUnit = "°C";
        if (tempState && tempState.state !== 'unknown' && tempState.state !== 'unavailable') {
            const parsed = parseFloat(tempState.state);
            tempText = !isNaN(parsed) ? Math.round(parsed) : tempState.state;
            if (tempState.attributes.unit_of_measurement) {
                tempUnit = tempState.attributes.unit_of_measurement.replace(/[^A-Za-z0-9°CF]/g, '');
            }
        }
        envHtml += `<span style="display:flex;align-items:center;"><ha-icon icon="mdi:thermometer" style="margin-right: 2px; color: #ff9800; --mdc-icon-size: 18px;"></ha-icon>${tempText}${tempUnit}</span>`;
        this._currentTempText = tempText !== "??" ? tempText + tempUnit : "";

        let humiText = "??";
        let humiUnit = "%";
        if (humiState && humiState.state !== 'unknown' && humiState.state !== 'unavailable') {
            const parsed = parseFloat(humiState.state);
            humiText = !isNaN(parsed) ? Math.round(parsed) : humiState.state;
            if (humiState.attributes.unit_of_measurement) {
                humiUnit = humiState.attributes.unit_of_measurement;
            }
        }
        envHtml += `<span style="display:flex;align-items:center;"><ha-icon icon="mdi:water-percent" style="margin-right: 2px; color: #03a9f4; --mdc-icon-size: 18px;"></ha-icon>${humiText}${humiUnit}</span>`;
        this._currentHumiText = humiText !== "??" ? humiText + humiUnit : "";

        this.envOverlay.innerHTML = envHtml;
        this.envOverlay.style.display = 'flex';
      }

      if (!this._initialized) {
        this._initialized = true;
        this.updateMusicStatus(hass, this._speakerEntityId, this._lullabyEntityId);
      }

      // Check if media library updated to automatically refresh the song list
      let currentLibraryStateObj = null;
      for (const key in hass.states) {
        if (key.startsWith('sensor.cuboai_media_library')) {
          currentLibraryStateObj = hass.states[key];
          break;
        }
      }
      const newLibraryStateStr = currentLibraryStateObj ? JSON.stringify(currentLibraryStateObj.attributes) : null;
      if (this._lastLibraryStateStr !== newLibraryStateStr) {
        this._lastLibraryStateStr = newLibraryStateStr;
        if (this.musicBar && typeof this._renderSongsFn === 'function') {
           this._renderSongsFn();
        }
      }

      if (this.musicBar && this._speakerEntityId) {
        this.updateMusicStatus(hass, this._speakerEntityId, this._lullabyEntityId);
      }
    } catch (err) {
      console.error(err);
      this.innerHTML = `<div style="background: #fee; border: 1px solid #fcc; color: #c00; padding: 15px; border-radius: 8px;"><h3>CuboAI Card Error</h3><p>${err.message}</p><pre style="overflow: auto; max-height: 150px;">${err.stack}</pre></div>`;
    }
  }

  setConfig(config) {
    try {
      if (!config) {
        throw new Error("Invalid configuration (config is undefined)");
      }
      this._config = config;
      if (this._userFilter === undefined) {
        this._userFilter = config.default_song_filter || 'all';
      }
      if (this._playlistUserFilter === undefined) {
        this._playlistUserFilter = config.default_playlist_filter || 'all';
      }
      if (this.config && this.config.device_id !== config.device_id) {
         // Config changed via editor, update child
         if (this.content && config.device_id) {
           let wEntity = null;
           let wRtspPort = 8555;
           if (this._hass && this._hass.states) {
               for (const e in this._hass.states) {
                   if (e.startsWith('camera.cuboai_') && e.endsWith('_local_camera')) {
                       wEntity = e;
                       if (this._hass.states[e].attributes && this._hass.states[e].attributes.rtsp_port) {
                           wRtspPort = this._hass.states[e].attributes.rtsp_port;
                       }
                       break;
                   }
               }
           }
           const webrtcConfig = {
             type: 'custom:webrtc-camera',
             entity: wEntity || '',
             url: wEntity ? undefined : `rtsp://127.0.0.1:${wRtspPort}/cuboai_combined_${config.device_id}`,
             mode: (navigator.vendor && navigator.vendor.includes('Apple')) ? 'mp4,hls,mse' : 'webrtc,mse',
             ui: true,
             muted: this.isMuted,
             media: this.micEnabled ? 'video,audio,microphone' : 'video,audio'
           };
           customElements.whenDefined('webrtc-camera').then(() => {
             this.content.setConfig(webrtcConfig);
           });
         } else if (this.content && !config.device_id) {
             // Fallback to auto-detect
             let deviceId = null;
             if (this._hass) {
               for (const entity_id in this._hass.states) {
                 if (entity_id.startsWith('media_player.cuboai_speaker_')) {
                   deviceId = entity_id.replace('media_player.cuboai_speaker_', '').toUpperCase();
                   break;
                 }
               }
             }
             if (deviceId) {
               let wEntity2 = null;
               let wRtspPort2 = 8555;
               if (this._hass && this._hass.states) {
                   for (const e in this._hass.states) {
                       if (e.startsWith('camera.cuboai_') && e.endsWith('_local_camera')) {
                           wEntity2 = e;
                           if (this._hass.states[e].attributes && this._hass.states[e].attributes.rtsp_port) {
                               wRtspPort2 = this._hass.states[e].attributes.rtsp_port;
                           }
                           break;
                       }
                   }
               }
               const webrtcConfig = {
                 type: 'custom:webrtc-camera',
                 entity: wEntity2 || '',
                 url: wEntity2 ? undefined : `rtsp://127.0.0.1:${wRtspPort2}/cuboai_combined_${deviceId}`,
                 mode: (navigator.vendor && navigator.vendor.includes('Apple')) ? 'mp4,hls,mse' : 'webrtc,mse',
                 ui: true,
                 muted: this.isMuted,
                 media: this.micEnabled ? 'video,audio,microphone' : 'video,audio'
               };
               customElements.whenDefined('webrtc-camera').then(() => {
                 this.content.setConfig(webrtcConfig);
               });
             }
         }
      }
      this.config = config;
    } catch (err) {
      console.error("CuboAI Card setConfig Error:", err);
      this._error = err;
    }
  }

  updateMusicStatus(hass, speakerEntityId, lullabyEntityId) {
    if (!this.musicBar) return;
    const speakerState = hass.states[speakerEntityId];
    const lullabyState = lullabyEntityId ? hass.states[lullabyEntityId] : null;
    
    if (speakerState && speakerState.attributes) {
      const haRepeat = speakerState.attributes.repeat;
      if (haRepeat !== undefined && this._repeatMode !== haRepeat) {
        this._repeatMode = haRepeat;
        const toggleRepeatBtn = this.musicBar ? this.musicBar.querySelector('#toggleRepeatBtn') : null;
        if (toggleRepeatBtn) {
          let rColor = this._repeatMode === 'off' ? 'inherit' : '#4caf50';
          let rIcon = this._repeatMode === 'one' ? 'mdi:repeat-once' : 'mdi:repeat';
          toggleRepeatBtn.innerHTML = `<ha-icon icon="${rIcon}" style="--mdc-icon-size: 14px; color: ${rColor};"></ha-icon> <span>Repeat: ${this._repeatMode.toUpperCase()}</span>`;
        }
      }
    }
    
    if (speakerEntityId) {
      const deviceId = speakerEntityId.split('_')[2];
      const timerState = hass.states['number.cuboai_speaker_timer_' + deviceId];
      if (timerState && this.musicBar) {
        const playTimeSelect = this.musicBar.querySelector('#playTimeSelect');
        if (playTimeSelect && playTimeSelect.value !== timerState.state) {
          playTimeSelect.value = timerState.state;
        }
      }
    }
    if (!speakerState) return;

    let activeState = speakerState.state;
    let activeAttributes = speakerState.attributes || {};
    let isLullabyPlaying = false;

    if (lullabyState && lullabyState.state === 'playing') {
      activeState = 'playing';
      activeAttributes = lullabyState.attributes || {};
      isLullabyPlaying = true;
    }
    
    const nowPlayingDiv = this.musicBar.querySelector('#nowPlaying');
    const nowPlayingText = this.musicBar.querySelector('#nowPlayingText');
    
    if (nowPlayingDiv && nowPlayingText) {
      if (activeState === 'playing') {
        let title = 'Unknown Song';
        if (isLullabyPlaying) {
          title = lullabyState.attributes.source || 'Lullaby';
        } else {
          const activeUrl = activeAttributes.media_content_id || activeAttributes.media_title;
          if (activeUrl) {
            let customSongs = [];
            try {
              let libraryState = null;
              if (hass.states) {
                for (const key in hass.states) {
                  if (key.startsWith('sensor.cuboai_media_library')) {
                    libraryState = hass.states[key];
                    break;
                  }
                }
              }
              if (libraryState && libraryState.attributes && libraryState.attributes.custom_songs) {
                customSongs = libraryState.attributes.custom_songs;
              }
              if (customSongs.length === 0) {
                const deviceId = speakerEntityId.split('_')[2];
                customSongs = JSON.parse(localStorage.getItem(`cuboai_custom_songs_${deviceId}`)) || [];
              }
            } catch(e) {}
            const song = customSongs.find(s => {
              if (s.url === activeUrl) return true;
              
              const getVid = (u) => {
                try {
                  const m = u.match(/(?:v=|\/)([0-9A-Za-z_-]{11})(?:&|\?|$)/);
                  return m ? m[1] : u;
                } catch(e) { return u; }
              };
              
              const vidS = getVid(s.url);
              const vidA = getVid(activeUrl);
              
              if (vidS.length === 11 && vidS === vidA) return true;
              
              return activeUrl.includes(s.url) || s.url.includes(activeUrl);
            });
            if (song) title = song.name;
            else title = activeUrl;
          } else {
            title = activeAttributes.media_title || 'Unknown Song';
          }
        }
        const artist = isLullabyPlaying ? 'CuboAI Lullaby' : (activeAttributes.media_artist || '');
        nowPlayingDiv.style.display = 'flex';
        nowPlayingText.textContent = `Now Playing: ${title}${artist ? ` - ${artist}` : ''}`;
      } else {
        nowPlayingDiv.style.display = 'none';
        
        // Reset playlist button if not playing anymore
        if (this._playlistActive && !this._isAdvancing) {
          this._playlistActive = false;
          this._currentPlaylist = [];
          this._queueIndex = -1;
          const playAllBtn = this.musicBar.querySelector('#playAllBtn');
          if (playAllBtn) {
            playAllBtn.textContent = 'Play';
            playAllBtn.style.background = '#4caf50';
          }
        }
      }
    }

    const previousState = this._lastSpeakerState;
    this._lastSpeakerState = activeState;

    if (this._playlistActive && previousState === 'playing' && activeState !== 'playing' && !this._isAdvancing) {
      this._isAdvancing = true;
      setTimeout(() => {
        const checkSpeaker = this._hass.states[speakerEntityId];
        const checkLullaby = lullabyEntityId ? this._hass.states[lullabyEntityId] : null;
        const stillPlaying = (checkSpeaker && checkSpeaker.state === 'playing') || (checkLullaby && checkLullaby.state === 'playing');
        
        if (!stillPlaying) {
          this.playNextQueueSong(speakerEntityId, lullabyEntityId);
        }
        this._isAdvancing = false;
      }, 2000);
    }
  }

  getCardSize() {
    return 3;
  }
}

if (!customElements.get('cuboai-camera-card')) {
  customElements.define('cuboai-camera-card', CuboAICameraCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some(c => c.type === 'cuboai-camera-card')) {
  window.customCards.push({
    type: 'cuboai-camera-card',
    name: 'CuboAI Camera',
    description: 'Zero-config CuboAI Live View with Two-Way Audio',
    preview: true
  });
}

