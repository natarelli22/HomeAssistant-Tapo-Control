## O que mudou nesta versão (`2026.11.270050`)

* **Traduções do seletor `live_view_mode` (`strings.json`, `translations/en.json`, `translations/pt.json`):**
  * Adicionado o bloco `selector.live_view_mode.options` em `strings.json`, `en.json` e `pt.json` para exibir corretamente os rótulos do modo de visualização ao vivo na UI também fora de `pt-BR`.
  * Removidas de `pt.json` as 4 chaves obsoletas de detecção de som (`enable_sound_detection`, `sound_detection_duration`, `sound_detection_peak`, `sound_detection_reset`).
* **Limpeza de imports não utilizados (`__init__.py`, `config_flow.py`, `media_source.py`, `osd_sync.py`):**
  * Removidos imports órfãos introduzidos ou deixados para trás em modificações do fork (`pathlib`, `SUBDIR_EVENTS`, `findMedia`, `getConfiguredColdDirPath`, `getDataPath`, `json`, `MEDIA_THUMBNAIL_CACHE`, `MEDIA_THUMBNAIL_PRELOAD`, `SD_SYNC_RECORDING_TYPES_EVENTS`, `getFileName`, `getRecordings`, `getWebFile`, `preloadRecordingThumbnails`, `cleanupThumbnailCache`, `re`, `time`).
