HTML_DOC_SOURCES = README USERGUIDE INSTALL CREDITS

docs/html/%.html: %.markdown Makefile
	mkdir -p docs/html
	( \
	echo "<html>"; \
	echo "<head><title>Passerd -" $* "</title></title>"; \
	echo "</head><body>"; \
	for f in $(HTML_DOC_SOURCES);do \
		echo "<a href=\"$$f.html\">$$f</a>"; \
	done; \
	echo "<a href=\"http://github.com/ehabkost/passerd\">CODE</a>"; \
	echo "<hl/>"; \
	markdown2 $<; \
	echo "</body>"; \
	) > "$@"

html-docs: $(patsubst %,docs/html/%.html,$(HTML_DOC_SOURCES))

upload-page: html-docs
	rsync -vaP docs/html/ passerd.raisama.net:passerd.raisama.net/
