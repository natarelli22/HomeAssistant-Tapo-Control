## O que mudou nesta versão

* **Tradução dos eventos de movimento:** As entidades de evento agora exibem o estado traduzido como *"Detectado"* no Home Assistant e no Diário de Bordo.
* **Sensor Recordings Synchronization:** Adicionado suporte ao modo Tapo Care com estados em tempo real (*"Tapo Care - Idle"*, *"Tapo Care - Cleaning"*, etc.) e atributos informativos detalhados (`last_deleted_count`, `last_cleanup_result`, `last_deleted_recordings`, `last_cleanup`).
* **Alinhamento de Retenção do Tapo Care:** O corte de retenção das gravações locais agora é calculado a partir do início do dia de calendário, mantendo alinhamento perfeito com a janela de busca do `tapo-care-backup` e impedindo o download de arquivos já expirados.
* **Registro no Diário de Bordo no Formato Local do HA:** As exclusões de gravações são registradas diretamente no Diário de Bordo e atributos com resumo limpo e agrupado por dia no formato de data do Home Assistant (ex: *`Tapo Care - Cleaned: 13/09/2026 (26 eventos)`*), com logs individuais preservados em debug.
* **Horário Diário de Limpeza do Tapo Care Configurável:** Adicionado campo obrigatório na configuração de mídia do Tapo Care para definir o horário diário exato (HH:MM) da limpeza, sem valor padrão, com validação e execução diária única no fuso horário local do Home Assistant.
* **Validação Inline do Cold Storage:** Mensagens de erro de caminho obrigatório ou inexistente do cold storage agora são exibidas diretamente acima do campo no próprio formulário de configuração de mídia.
