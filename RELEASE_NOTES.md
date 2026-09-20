## O que mudou nesta versão

* **Tradução dos eventos de movimento:** As entidades de evento agora exibem o estado traduzido como *"Detectado"* no Home Assistant e no Diário de Bordo.
* **Sensor Recordings Synchronization:** Adicionado suporte ao modo Tapo Care com estados em tempo real (*"Tapo Care - Idle"*, *"Tapo Care - Cleaning"*, etc.) e atributos informativos detalhados (`last_deleted_count`, `last_cleanup_result`, `last_deleted_recordings`, `last_cleanup`, `cleanup_interval_hours`, `next_cleanup`).
* **Intervalo de Limpeza Configurável (Tapo Care):** Nova entidade numérica `Tapo Care Cleanup Interval` permitindo ajustar a periodicidade da limpeza em horas (padrão 24h), evitando execuções desnecessárias a cada minuto.
* **Registro no Diário de Bordo e Logs Otimizados:** Exclusões de gravações são registradas diretamente no Diário de Bordo (Logbook) da entidade com detalhes das gravações removidas, mantendo os logs do sistema limpos em nível de debug.
