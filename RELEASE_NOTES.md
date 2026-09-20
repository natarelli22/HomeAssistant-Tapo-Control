## O que mudou nesta versão

* **Tradução dos eventos de movimento:** As entidades de evento agora exibem o estado traduzido como *"Detectado"* no Home Assistant e no Diário de Bordo.
* **Sensor Recordings Synchronization:** Adicionado suporte ao modo Tapo Care com estados em tempo real (*"Tapo Care - Idle"*, *"Tapo Care - Cleaning"*, etc.) e atributos informativos (`last_deleted_count`, `last_cleanup_result`, `last_cleanup`) tanto no modo Tapo Care quanto no modo Cartão SD.
* **Limpeza e Logs Informativos:** Arquivos e diretórios expirados excluídos durante a rotina de limpeza agora são registrados em nível `INFO` nos logs do Home Assistant, e a quantidade de arquivos removidos é exposta diretamente nos atributos da entidade.
