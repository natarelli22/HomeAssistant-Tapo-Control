## O que mudou nesta versão (`2026.11.262219`)

* **Fast Download como único método de download do cartão SD (`config_flow.py`, `const.py`, `__init__.py`, `utils.py`, `media_source.py`, `sensor.py`):**
  * Removida a opção de escolha manual entre o método legado e o **Fast Download** nas opções de mídia do cartão SD (`async_step_media_sd`), fixando `SD_DOWNLOAD_METHOD_FAST` como padrão e removendo as constantes `SD_DOWNLOAD_METHOD_LEGACY` e `SD_DOWNLOAD_METHOD_OPTIONS`.
  * Removidos o loop legado de sincronização no `mediaSync`, as checagens condicionais de modo legado na limpeza/organização de armazenamento (`mediaCleanup`, `reorganize_media_storage`, `getColdFile`), no navegador de mídia (`media_source.py`) e no sensor de próxima limpeza (`sensor.py`), mantendo leitura retrocompatível de arquivos existentes na raiz do diretório.
  * Mantido apenas o fallback automático interno em `getRecording()` para o downloader legado em rejeições não transitórias de protocolo para clipes curtos (`<= 120s`).
* **Correções de pontas soltas e estabilidade (`config_flow.py`, `utils.py`):**
  * **Tapo Care (`config_flow.py`):** Inicializadas as variáveis `thumbnail_cache` e `thumbnail_preload` em `async_step_media_tapo_care()` a partir de `self.config_entry.data` (inclusive em re-exibição após erro de validação), corrigindo potencial `NameError` ao salvar opções do Tapo Care.
  * **Reload de opções e timestamp (`utils.py`):** Protegidos `update_listener()` e `setupOnvif()` contra `KeyError: 'name'` durante reloads usando acesso seguro ao `hass.data[DOMAIN]` com fallback para `entry.title`/`entry.entry_id`, e substituído `datetime.utcnow().timestamp()` por `time.time()` em `update_listener()`.
* **Limpeza de traduções (`strings.json`, `translations/en.json`, `translations/pt.json`, `translations/pt-BR.json`):**
  * Removidas as entradas e seletores de `sd_download_method` de `strings.json` e das traduções.
  * Removidas chaves obsoletas (`enable_stream`, `enable_sound_detection`, `sound_detection_*`) em `pt-BR.json` e adicionadas as traduções ausentes `options.error.invalid_go2rtc_api_url` e `options.step.auth.data_description.live_view_mode`.
