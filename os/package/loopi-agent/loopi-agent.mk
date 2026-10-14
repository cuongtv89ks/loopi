################################################################################
#
# loopi-agent
#
################################################################################

LOOPI_AGENT_VERSION = 0.1.0
LOOPI_AGENT_SITE = $(BR2_EXTERNAL_LOOPI_PATH)/../agent
LOOPI_AGENT_SITE_METHOD = local
LOOPI_AGENT_LICENSE = Proprietary
LOOPI_AGENT_DEPENDENCIES = host-rustc host-pkgconf alsa-lib opus

# Same environment as Buildroot's cargo-package infrastructure (cross linker, target triple,
# pkg-config sysroot). Crates are fetched from crates.io; Cargo.lock pins every version.
# PKG_CARGO_ENV also overrides the crate's [profile.release] through CARGO_PROFILE_RELEASE_*: opt-level 2
# (BR2_OPTIMIZE_2), no LTO (BR2_ENABLE_LTO unset), 16 codegen units, no debug info. Deliberate (ruling C38):
# speed on the ARM1176 over binary size; the crate's own opt-level "s" + LTO only applies to host builds.
define LOOPI_AGENT_BUILD_CMDS
	cd $(@D) && $(TARGET_MAKE_ENV) $(TARGET_CONFIGURE_OPTS) $(PKG_CARGO_ENV) \
		PKG_CONFIG_ALLOW_CROSS=1 \
		cargo build --release --locked --bin loopi-agent
endef

define LOOPI_AGENT_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/target/$(RUSTC_TARGET_NAME)/release/loopi-agent \
		$(TARGET_DIR)/usr/bin/loopi-agent
endef

$(eval $(generic-package))
