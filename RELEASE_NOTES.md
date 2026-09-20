## O que mudou nesta versão

* **Tradução dos eventos de movimento:** As entidades de evento agora exibem o estado traduzido como *"Detectado"* no Home Assistant e no Diário de Bordo.
* **Sensor Recordings Synchronization:** Adicionado suporte ao modo Tapo Care com estados em tempo real (*"Tapo Care - Idle"*, *"Tapo Care - Cleaning"*, etc.) e atributos informativos detalhados (`last_deleted_count`, `last_cleanup_result`, `last_deleted_recordings`, `last_cleanup`) tanto no modo Tapo Care quanto no modo Cartão SD.
* **Limpeza e Logs Informativos com Nome do Dispositivo:** Exclusões de gravações e diretórios expirados agora identificam o nome amigável da câmera (ex: `Sala_02`) em nível `INFO` nos logs do sistema e registram mensagens detalhadas no Diário de Bordo (Logbook) do Home Assistant.
