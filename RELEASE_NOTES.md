## O que mudou nesta versão

* **Correção de Chamada Bloqueante no Event Loop (`asyncio`):** Resolvido o erro de `Detected blocking call to open` associado ao carregamento e leitura de dados de locale do Babel na thread principal do Home Assistant.
* **Formatação de Datas 100% Dinâmica via Home Assistant:** As datas agora são formatadas dinamicamente pela biblioteca oficial de internacionalização do Home Assistant (Babel / Unicode CLDR) a partir de `hass.config.language` e `hass.config.country`, eliminando qualquer regra fixa (*hardcode*).
* **Pré-aquecimento no Startup:** Inicialização e carregamento dos dados de locale executados em segundo plano via thread pool executor (`async_add_executor_job`) durante o `async_setup_entry`.
* **Processamento Completo de Limpeza no Executor:** A rotina `mediaCleanup` agora executa a exclusão de arquivos e a formatação das listas de deletados e sumário em conjunto no executor de threads, mantendo o event loop 100% livre e responsivo.
