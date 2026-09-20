## O que mudou nesta versão

* **Nova entidade de evento (`event`):** Adicionada entidade nativa de movimento (`event.<camera>_cell_motion_detection`) vinculada diretamente à câmera.
* **Histórico de atividades limpo:** Registra apenas os disparos de detecção de movimento, eliminando o registro de *"Não detectado"* no histórico e no Diário de Bordo.
* **Compatibilidade mantida:** O sensor binário tradicional (`binary_sensor`) continua existindo para automações que dependem de tempo contínuo.
