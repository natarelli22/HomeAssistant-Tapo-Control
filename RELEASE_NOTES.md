# 🚀 Tapo: Cameras Control - Notas da Versão

Esta versão introduz suporte completo à sincronização e reprodução de gravações da nuvem (Tapo Care / Cold Storage), novas telas de aviso no fluxo de configuração para prevenção de erros e correções de estabilidade no Home Assistant.

---

## 🌟 Principais Funcionalidades Adicionadas

### 1. Suporte a Sincronização Dupla: SD Card e Tapo Care (Cold Storage)
* **Reprodução e Navegação Local:** O `media_source.py` foi expandido para permitir navegar e reproduzir vídeos armazenados localmente no diretório de *Cold Storage* (vídeos baixados do Tapo Care), mantendo suporte integral e simultâneo ao cartão SD.
* **Política de Retenção Automática (`retention_days`):** Implementada rotina periódica em `utils.py` para limpeza automática de vídeos antigos no Cold Storage, garantindo que gravações anteriores ao período configurado sejam excluídas sem intervenção manual.
* **Entidades de Controle:** Novos switches em `switch.py` permitindo ativar/desativar a sincronização de mídia e o backup do Tapo Care individualmente por câmera.

---

## 🛡️ Melhorias de Usabilidade e Telas Dedicadas (Config Flow)

### 2. Validação e Telas de Aviso Dedicadas
* **Verificação Prévia do Cartão MicroSD:**
  * Ao selecionar o armazenamento via SD Card, a integração consulta a câmera (`controller.getRecordingsList()`) antes de salvar as configurações.
  * Se a câmera não possuir cartão ou retornar o código `-71114` (`STORAGE_NOT_EXIST`), a configuração **não é salva com erro**. Em vez disso, o usuário é redirecionado para a tela dedicada **`media_no_sd`** (*"Cartão SD Não Encontrado"*), orientando a inserir um cartão ou optar pelo Tapo Care.
* **Validação Obrigatória do Cold Storage:**
  * Ao configurar o Tapo Care, a integração verifica se o caminho da pasta local foi informado e se o diretório existe no disco.
  * Se o caminho estiver em branco ou não existir, o fluxo redireciona para a tela dedicada **`media_tapo_care_error`** (*"Caminho do Cold Storage Obrigatório"*), preservando os valores já digitados para correção imediata.

---

## 🐛 Correções de Bugs e Compatibilidade

### 3. Resiliência no Descarregamento de Entidades
* **Tratamento de `ValueError: Config entry was never loaded!`:**
  * No `__init__.py` (`async_unload_entry`), o descarregamento das plataformas (ex: `binary_sensor`) agora é feito de forma protegida individualmente. Se uma plataforma não tiver sido carregada para a câmera, a exceção é interceptada e registrada em nível debug, evitando falhas e mensagens de erro no log do Home Assistant durante alterações de configuração ou reinicializações.

### 4. Correção de Aviso de `entity_id` no Media Source
* **Compatibilidade com Home Assistant 2026+:**
  * Em `media_source.py`, a chamada `resolve_media_source` foi ajustada passando explicitamente `target_entity_id=None`, eliminando o aviso emitido pelo `homeassistant.helpers.frame`.

---

## 🌐 Internacionalização e Traduções

### 5. Traduções Atualizadas
* Adicionadas todas as mensagens, títulos e instruções das novas telas nos idiomas:
  * **Português do Brasil (`pt-BR.json`)**
  * **Português (`pt.json`)**
  * **Inglês (`en.json` e `strings.json`)**
