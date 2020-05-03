all: fxs
.PHONY: all

# Include standard NCS examples build definitions and rules
include $(NCS_DIR)/src/ncs/build/include.ncs.mk

SRC = $(wildcard yang/*.yang)
DIRS = ../load-dir java/src/$(JDIR)/$(NS)
FXS = $(SRC:yang/%.yang=../load-dir/%.fxs)

## Uncomment and patch the line below if you have a dependency to a NED
## or to other YANG files
YANGPATH += ../../cisco-etsi-nfvo/src/yang 
YANGPATH += ../../itd-service/src/yang 
YANGPATH += ../../service-base/src/yang 
# 	../../<pkt-name>/src/yang

NCSCPATH   = $(YANGPATH:%=--yangpath %)
YANGERPATH = $(YANGPATH:%=--path %)
PYTHONSRC = $(wildcard ../python/ftdv_ngfw/*.py)
PYTHONBUILD = $(wildcard ../python/ftdv_ngfw/*.pyc)
TEMPLATESRC = $(wildcard loaddata/*.xml)

fxs: $(PYTHONSRC) $(PYTHONBUILD) $(DIRS) $(FXS) $(TEMPLATESRC)

$(PYTHONSRC): .FORCE
	python -m py_compile $@
.PHONY: .FORCE
$(PYTHONBUILD): .FORCE2
	rm -f $@
.PHONY: .FORCE2

$(DIRS):
	mkdir -p $@

../load-dir/%.fxs: yang/%.yang
	$(NCSC)  `ls $*-ann.yang  > /dev/null 2>&1 && echo "-a $*-ann.yang"` \
             $(NCSCPATH) -c -o $@ $<

$(TEMPLATESRC): .FORCE
	cp $@ ../load-dir

clean:
	rm -rf $(DIRS)
.PHONY: clean
