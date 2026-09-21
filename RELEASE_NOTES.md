## O que mudou nesta versão

* **Limpeza em Lotes (Batch Processing):** A rotina de limpeza agora processa arquivos expirados em blocos de até 200 gravações por ciclo (com pausa assíncrona de 2 segundos entre eles), aliviando o I/O de armazenamento e o event loop do Home Assistant.
* **Atributo `Last deleted recordings total`:** Novo atributo no sensor `Recordings Synchronization` que acumula a quantidade total de gravações deletadas no dia, posicionado imediatamente após `Last deleted recordings`.
* **Resultado Pontual em `Last cleanup result`:** Exibe o resultado e a contagem pontual da rodada de limpeza atual (sem misturar ou somar textos de rodadas anteriores).
* **Remoção de Redundâncias:** Unificada a montagem dos atributos em `sensor.py` e eliminados códigos mortos e imports não utilizados em `utils.py`, `event.py` e `media_source.py`.
