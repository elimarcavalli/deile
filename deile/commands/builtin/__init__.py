"""Comandos builtin do DEILE.

Os comandos são descobertos no filesystem por
:meth:`deile.commands.registry.CommandRegistry.auto_discover_builtin_commands`
— qualquer arquivo ``*_command.py`` neste diretório que NÃO comece com ``_``
é importado e suas subclasses concretas de ``SlashCommand`` registradas
automaticamente. Não há lista a manter aqui.
"""
