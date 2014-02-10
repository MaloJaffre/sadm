Setup instructions
==================

If you are like the typical Prologin organizer, you're probably reading this
documentation one day before the start of the event, worried about your ability
to make everything work before the contest starts. Fear not! This section of
the documentation explain everything you need to do to set up the
infrastructure for the finals, assuming all the machines are already physically
present. Just follow the guide!

Last update: 2013.

Step 0: hardware and network setup
----------------------------------

Before installing servers, we need to make sure all the machines are connected
to the network properly. Here are the major points you need to be careful
about:

* Make sure to balance the number of machines connected per switch: the least
  machines connected to a switch, the better performance you'll get.
* Inter-switch connections is not very important: we tried to make most things
  local to a switch (RFS + HFS should each be local, the rest is mainly HTTP
  connections to services).

For each pair of switches, you will need one RHFS server (connected to the 2
switches via 2 separate NICs, and hosting the RFS + HFS for the machines on
these 2 switches). Please be careful out the disk space: assume that each RHFS
has about 100GB usable for HFS storage. That means at most 50 contestants (2GB
quota) or 20 organizers (5GB quota) per RHFS. With contestants that should not
be a problem, but try to balance organizers machines as much as possible.

Step 1: setting up the core services: MDB, DNS, DHCP
----------------------------------------------------

This is the first and trickiest part of the setup. As this is the core of the
architecture, everything kind of depends on each other:

.. image:: core-deps.png

Fortunately, we can easily work around these dependencies in the beginning.

All these core services will be running on ``gw.prolo``, the network gateway.
They could run elsewhere but we don't have a lot of free machines and the core
is easier to set up at one single place.

Basic system
~~~~~~~~~~~~

Start by installing a standard Arch system. We're going to have some network
related stuff to do, so rename the LAN interface to something with a
readable name::

  echo 'SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="aa:bb:cc:dd:ee:ff",
  NAME="lan"' >> /etc/udev/rules.d/10-network.rules

Install a few packages we will need::

  pacman -S git dhcp bind python python-pip python-virtualenv libyaml nginx \
            sqlite dnsutils rsync postgresql-libs tcpdump base-devel pwgen

Create the main Python ``virtualenv`` we'll use for all our Prologin apps::

  mkdir /var/prologin
  virtualenv3 --no-site-packages /var/prologin/venv

Enter the ``virtualenv``::

  source /var/prologin/venv/bin/activate

Clone the ``sadm`` repository, which contains everything we'll need to setup::

  git clone http://bitbucket.org/prologin/sadm
  cd sadm

Install the required Python packages::

  pip install -r requirements.txt

mdb
~~~

We now have a basic environment to start setting up services on our gateway
server. We're going to start by installing ``mdb`` and configuring ``nginx`` as
a reverse proxy for this application. Fortunately, a very simple script is
provided with the application in order to setup what it requires::

  python3 install.py mdb
  # Type 'no' when Django asks you to create a superuser.
  mv /etc/nginx/nginx.conf{.new,}
  # ^ To replace the default configuration by our own.

This command installed the ``mdb`` application to ``/var/prologin/mdb`` and
installed the ``systemd`` and ``nginx`` configuration files required to run the
application.

Don't forget to change the ``secret_key`` used by Django and the
``shared_secret`` used for ``mdb`` to ``mdbsync`` pushes::

  $EDITOR /etc/prologin/mdb-server.yml
  $EDITOR /etc/prologin/mdbsync-pub.yml

You should be able to start ``mdb`` and ``nginx`` like this::

  systemctl enable mdb && systemctl start mdb
  systemctl enable nginx && systemctl start nginx

In order to test if ``mdb`` is working properly, we need to go to query
``http://mdb/`` with a command line tool like ``curl``. However, to get DNS
working, we need ``mdbdns``, which needs ``mdbsync``, which needs ``mdb``. As a
temporary workaround, we're going to add ``mdb`` to our ``/etc/hosts`` file::

  echo '127.0.0.1 mdb' >> /etc/hosts

Now you should get an empty list when querying ``/query``::

  curl http://mdb/query
  # Should return []

Congratulations, ``mdb`` is installed and working properly!

mdbsync
~~~~~~~

The next step now is to setup ``mdbsync``. ``mdbsync`` is a Tornado web server
used for applications that need to react on ``mdb`` updates. The DHCP and DNS
config generation scripts use it to automatically update the configuration when
``mdb`` changes. Once again, setting up ``mdbsync`` is pretty easy::

  python3 install.py mdbsync

  systemctl enable mdbsync && systemctl start mdbsync
  systemctl reload nginx
  echo '127.0.0.1 mdbsync' >> /etc/hosts

To check if ``mdbsync`` is working, try to register for updates::

  python -c 'import prologin.mdbsync; prologin.mdbsync.connect().poll_updates(print)'
  # Should print {} {} and wait for updates

mdbdns
~~~~~~

``mdbdns`` gets updates from ``mdbsync`` and regenerates the DNS configuration.
Once again, an installation script is provided::

  python3 install.py mdbdns
  mv /etc/named.conf{.new,}
  # ^ To replace the default configuration by our own.
  systemctl enable mdbdns && systemctl start mdbdns
  systemctl enable named && systemctl start named

We now need to add a record in ``mdb`` for our current machine, ``gw.prolo``,
so that DNS configuration can be generated::

  cd /var/prologin/mdb
  python3 manage.py addmachine --hostname gw --mac 11:22:33:44:55:66 \
      --ip 192.168.1.254 --rfs 0 --hfs 0 --mtype service --room pasteur \
      --aliases mdb,mdbsync,ns,netboot,udb,udbsync,presencesync

Once this is done, ``mdbdns`` should have automagically regenerated the DNS
configuration::

  host mdb.prolo 127.0.0.1
  # Should return 192.168.1.254

You can now remove the two lines related to ``mdb`` and ``mdbsync`` from your
``/etc/hosts`` file, and configure ``/etc/resolv.conf`` to use ``127.0.0.1`` as
your default DNS server::

  cat > /etc/resolv.conf <<EOF
  search prolo
  nameserver 127.0.0.1
  EOF

mdbdhcp
~~~~~~~

``mdbdhcp`` works just like ``mdbdns``, but for DHCP. The installation steps
are as usual::

  python3 install.py mdbdhcp
  mv /etc/dhcpd.conf{.new,}
  # ^ To replace the default configuration by our own.
  systemctl enable mdbdhcp && systemctl start mdbdhcp
  systemctl enable dhcpd4 && systemctl start dhcpd4

netboot
~~~~~~~

Netboot is a small HTTP service used to handle interactions with the PXE boot
script: machine registration and serving kernel files. Once again, very simple
setup::

  python3 install.py netboot
  systemctl enable netboot && systemctl start netboot
  systemctl reload nginx

TFTP
~~~~

The TFTP server is used by the PXE clients to fetch the first stage of the boot
chain: the iPXE binary (more on that in the next section). We simply setup
``tftp-hpa``::

  pacman -S tftp-hpa
  systemctl enable tftpd.socket && systemctl start tftpd.socket

The TFTP server will serve files from ``/srv/tftp``.

iPXE bootrom
~~~~~~~~~~~~

The iPXE bootrom is an integral part of the boot chain for user machines. It is
loaded by the machine BIOS via PXE and is responsible for booting the Linux
kernel using the nearest RFS. It also handles registering the machine in the
MDB if needed. These instructions need to be run on ``gw``.

iPXE is an external open source project, clone it first::

  git clone git://git.ipxe.org/ipxe.git

Then compile time settings need to be modified. Uncomment the following lines::

  // in src/config/general.h
  #define REBOOT_CMD

You can now build iPXE: go to ``src/`` and build the bootrom using our script
provided in ``sadm/netboot``::

  make bin/undionly.kpxe EMBED=/root/sadm/netboot/script.ipxe
  cp bin/undionly.kpxe /srv/tftp/prologin.kpxe

udb
~~~

Install ``udb`` using the ``install.py`` recipe::

  python install.py udb
  systemctl enable udb && systemctl start udb
  systemctl reload nginx

You can then import all contestants information to ``udb`` using the
``batchimport`` command::

  cd /var/prologin/udb
  python manage.py batchimport --file=/root/finalistes.txt

The password sheet data can then be generated with this command, then printed
by someone else::

  python manage.py pwdsheetdata --type=user > /root/user_pwdsheet_data

Then do the same for organizers::

  python manage.py batchimport --logins --type=orga --pwdlen=10 \
      --uidbase=11000 --file=/root/orgas.txt
  python manage.py pwdsheetdata --type=orga > /root/orga_pwdsheet_data

udbsync
~~~~~~~

Again, use the ``install.py`` recipe::

  python install.py udbsync
  systemctl enable udbsync && systemctl start udbsync
  systemctl reload nginx

We can then configure udbsync clients::

  python install.py udbsync_django udbsync_rootssh
  systemctl enable udbsync_django@mdb && systemctl start udbsync_django@mdb
  systemctl enable udbsync_django@udb && systemctl start udbsync_django@udb
  systemctl enable udbsync_rootssh && systemctl start udbsync_rootssh

presencesync
~~~~~~~~~~~~

And once again::

  python install.py presencesync
  systemctl enable presencesync && systemctl start presencesync

Step 2: file storage
--------------------

TODO: setting up ``rhfs0`` + instructions to setup other ``rhfs`` machines and
sync them.

Step 3: booting the user machines
---------------------------------

Note: if you are good at typing on two keyboards at once, or you have a spare
root doing nothing, this step can be done in parallel with step 4.

Installing the base user system
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _ArchLinux Diskless Installation: https://wiki.archlinux.org/index.php/Diskless_network_boot_NFS_root#Bootstrapping_installation

The basic install process is already documented through the
`ArchLinux Diskless Installation`_ for conveniance a transcript is provided
below with a more up to date technique.

After installing the base system with at least base package, you have to
install the base system for diskless client system::

  export ROOTFS=/export/nfsroot
  export SUBNET=192.168.0.0/24
  pacman -Sy devtools nfs-utils openssh
  mkdir -p $ROOTFS
  for svc in {sshd,nfsd,rpc-{idmapd,gssd,mountd,statd}}.service; do
    systemctl enable $svc
    systemctl start  $svc
  done
  mkarchroot $ROOTFS base mkinitcpio-nfs-utils nfs-utils openssh strace tcpdump bash
  arch-chroot $ROOTFS bash
  ln -s /usr/share/zoneinfo/Europe/Paris /etc/localtime
  sed -e 's:^#en_US:en_US:g' -e 's:^#fr_FR:fr_FR:g' -i /etc/locale.gen
  sed -e 's:^HOOKS.*:HOOKS="base udev autodetect modconf net block filesystems keyboard fsck":g' \
      -e 's:^MODULES.*:MODULES="nfsv3":g' -i /etc/mkinitcpio.conf
  sed -e 's:^CheckSpace:#CheckSpace:' -e 's:^SigLevel.*:SigLevel = Never:' -i /etc/pacman.conf
  echo LANG=en_US.UTF-8 > /etc/locale.conf
  echo KEYMAP=us > /etc/vconsole.conf
  locale-gen
  mkinitcpio -p linux
  for svc in {sshd}.service; do
    systemctl enable $svc
    systemctl start  $svc
  done
  echo "$ROOTFS $SUBNET(ro,no_root_squash,subtree_check,async)" > /etc/exports.d/rootfs.exports

TODO: How to install new package, sync, hook to generate /var... and more
documentation to the above commands.

Copying the kernel and initramfs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

TODO: basically, take the kernel+initrd from the nfsroot and put it in
``/srv/tftp`` on ``gw``.

Step 4: setting up the web services
-----------------------------------

The web services will usually be set up on a separate machine from the ``gw``,
for availability and performance reasons (all services on ``gw`` are critical,
so you wouldn't want to mount a NFS on it for example). This machine is usually
named ``web.prolo``.

Once again, set up a standard Arch system. Then register it on ``mdb``, via the
web interface, or using::

  source /var/prologin/venv/bin/activate
  cd /var/prologin/mdb
  python3 manage.py addmachine --hostname web --mac 11:22:33:44:55:66 \
      --ip 192.168.1.100 --rfs 0 --hfs 0 \
      --aliases concours,wiki,bugs,docs,home,paste,map \
      --mtype service --room pasteur

When this is done, reboot ``web``: it should get the correct IP address from
the DHCP server, and should be able to access the internet. Proceed to setup a
virtualenv in ``/var/prologin/venv`` and clone the sadm repository by following
the same instructions given for ``gw`` ("Basic system" part).

Then, install the ``nginx`` configuration from the repository::

  python3 install.py nginxcfg
  mv /etc/nginx/nginx.conf{.new,}
  systemctl enable nginx && systemctl start nginx

Autoinstall
~~~~~~~~~~~

You can autoinstall some services and configuration files::

  python3 install.py webservices
  systemctl reload nginx

doc
~~~

You have to retrieve the documentations of each language::

  pacman -S wget unzip
  cd /var/prologin/webservices/docs
  su webservices # So we don't have to change permissions afterwards
  ./get_docs.sh

TODO: stechec2 docs, sadm docs

paste
~~~~~

You just have to start the ``paste`` service::

  systemctl enable paste && systemctl start paste

wiki
~~~~

Download and install the MoinMoin archlinux package, and its dependancies::

  pacman -S python2 moinmoin gunicorn
  mkdir -p /var/prologin/wiki
  cp -r /usr/share/moin /var/prologin/wiki/

Then install the WSGI file::

  cd /var/prologin/wiki/moin
  cp server/moin.wsgi ./moin.py

Edit ``moin.py`` to set the path to the wiki configuration directory:
uncomment the line after ``a2)`` and modify it like this::

  sys.path.insert(0, '/var/prologin/wiki/moin')

Copy the wiki configuration file::

  cp webservices/wiki/wikiconfig.py /var/prologin/wiki

Fix permissions::

  chown -R webservices:webservices /var/prologin/wiki
  chmod o-rwx -R /var/prologin/wiki

Create the ``prologin`` super-user::

  PYTHONPATH=/var/prologin/wiki:$PYTHONPATH                              \
  moin --config-dir=/var/prologin/wiki account create --name prologin    \
       --alias prologin --password **CHANGEME** --email prologin@example.com

Add users in the sadm folder (TODO: will be obsolete with udbsync)::

  webservices/wiki/create_users.sh < passwords.txt

Then you can just start the service::

  systemctl enable wiki && systemctl start wiki

bugs
~~~~

Install redmine and its dependancies::

  pacman -S ruby ruby-bundler redmine
  gem install unicorn

Move the redmine folder to /var/prologin, and the configuration to /etc::

  cp -r /usr/share/webapps/redmine /var/prologin/bugs
  cp webservices/redmine/redmine.ru /etc/unicorn/
  cd /var/prologin/bugs

Then execute these PostgreSQL queries to create the redmine DB::

  CREATE ROLE redmine LOGIN ENCRYPTED PASSWORD '**CHANGEME**' NOINHERIT VALID
  UNTIL 'infinity';
  CREATE DATABASE redmine WITH ENCODING='UTF8' OWNER=redmine;

Edit the configuration::

  cp database.yml.example database.yml
  $EDITOR database.yml

A configuration example::

  production:
    adapter: postgresql
    database: redmine
    host: localhost
    username: redmine
    password: **CHANGEME**
    encoding: utf8
    schema_search_path: public

Install required gems::

  bundle install --without development test

Generate the secret token::

  rake generate_secret_token

Fix permissions::

  chown -R redmine:redmine /var/prologin/bugs
  chmod o-rwx -R /var/prologin/bugs
  su redmine

Create the database structure and populate it with the default data::

  RAILS_ENV=production rake db:migrate
  RAILS_ENV=production REDMINE_LANG=fr-FR rake redmine:load_default_data
  
Set the FS permissions::

  mkdir -p tmp tmp/pdf public/plugin_assets
  chown -R redmine:redmine files log tmp public/plugin_assets
  chmod -R 775 files log tmp tmp/pdf public/plugin_assets

Then start the service::

  systemctl enable bugs && systemctl start bugs

Homepage
~~~~~~~~

The homepage links to all our web services. It is a simple Django app that
allows adding links easily. Setup it using ``install.py``::

  python install.py homepage
  systemctl enable homepage && systemctl start homepage
  systemctl enable udbsync_django@homepage
  systemctl start udbsync_django@homepage

Step 5: the matches cluster
---------------------------

TODO
