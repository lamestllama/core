#!/bin/sh
# auto-generated by zebra service (quagga.py)
QUAGGA_CONF="${quagga_conf}"
QUAGGA_SBIN_SEARCH="${quagga_sbin_search}"
QUAGGA_BIN_SEARCH="${quagga_bin_search}"
QUAGGA_STATE_DIR="${quagga_state_dir}"

searchforprog()
{
    prog=$1
    searchpath=$@
    ret=
    for p in $searchpath; do
        if [ -x $p/$prog ]; then
            ret=$p
            break
        fi
    done
    echo $ret
}

confcheck()
{
    CONF_DIR=`dirname $QUAGGA_CONF`
    # if /etc/quagga exists, point /etc/quagga/Quagga.conf -> CONF_DIR
    if [ "$CONF_DIR" != "/etc/quagga" ] && [ -d /etc/quagga ] && [ ! -e /etc/quagga/Quagga.conf ]; then
        ln -s $CONF_DIR/Quagga.conf /etc/quagga/Quagga.conf
    fi
    # if /etc/quagga exists, point /etc/quagga/vtysh.conf -> CONF_DIR
    if [ "$CONF_DIR" != "/etc/quagga" ] && [ -d /etc/quagga ] && [ ! -e /etc/quagga/vtysh.conf ]; then
        ln -s $CONF_DIR/vtysh.conf /etc/quagga/vtysh.conf
    fi
}

bootdaemon()
{
    QUAGGA_SBIN_DIR=$(searchforprog $1 $QUAGGA_SBIN_SEARCH)
    if [ "z$QUAGGA_SBIN_DIR" = "z" ]; then
        echo "ERROR: Quagga's '$1' daemon not found in search path:"
        echo "  $QUAGGA_SBIN_SEARCH"
        return 1
    fi

    flags=""

    if [ "$1" = "xpimd" ] && \\
        grep -E -q '^[[:space:]]*router[[:space:]]+pim6[[:space:]]*$' $QUAGGA_CONF; then
        flags="$flags -6"
    fi

    $QUAGGA_SBIN_DIR/$1 $flags -d
    if [ "$?" != "0" ]; then
        echo "ERROR: Quagga's '$1' daemon failed to start!:"
        return 1
    fi
}

bootquagga()
{
    QUAGGA_BIN_DIR=$(searchforprog 'vtysh' $QUAGGA_BIN_SEARCH)
    if [ "z$QUAGGA_BIN_DIR" = "z" ]; then
        echo "ERROR: Quagga's 'vtysh' program not found in search path:"
        echo "  $QUAGGA_BIN_SEARCH"
        return 1
    fi

    # fix /var/run/quagga permissions
    id -u quagga 2>/dev/null >/dev/null
    if [ "$?" = "0" ]; then
        chown quagga $QUAGGA_STATE_DIR
    fi
    sleep 2
    bootdaemon "zebra"
    for r in rip ripng ospf6 ospf bgp babel; do
        if grep -q "^router \\<$${}{r}\\>" $QUAGGA_CONF; then
            bootdaemon "$${}{r}d"
        fi
    done

    if grep -E -q '^[[:space:]]*router[[:space:]]+pim6?[[:space:]]*$' $QUAGGA_CONF; then
        bootdaemon "xpimd"
    fi

    $QUAGGA_BIN_DIR/vtysh -b
}

if [ "$1" != "zebra" ]; then
    echo "WARNING: '$1': all Quagga daemons are launched by the 'zebra' service!"
    exit 1
fi
confcheck
bootquagga
