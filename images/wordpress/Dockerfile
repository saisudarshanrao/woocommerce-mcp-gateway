# WordPress + WooCommerce store image.
#
# The official image supplies Apache, PHP and WordPress core. This layer adds
# what every store pod needs and nothing that differs between stores:
#   - WP-CLI, used by the setup init container
#   - WooCommerce and the Storefront theme as pinned, checksummed zips, so a pod
#     never downloads code at start
#   - a wp-config.php that takes every per-store value from the environment
#   - Apache on port 8080 so the container runs as a non-root user
ARG WORDPRESS_VERSION=7.1.3
ARG PHP_VERSION=8.3
FROM wordpress:${WORDPRESS_VERSION}-php${PHP_VERSION}-apache

ARG WP_CLI_VERSION=2.12.0
ARG WP_CLI_SHA256=ce34ddd838f7351d6759068d09793f26755463b4a4610a5a5c0a97b68220d85c
ARG WOOCOMMERCE_VERSION=11.2.0
ARG WOOCOMMERCE_SHA256=66e91b45b057b64c73d4521111cea5bdc78c4c89de8fa2ca4b18b9422706d722
ARG STOREFRONT_VERSION=4.6.2
ARG STOREFRONT_SHA256=163b5bf217dad76a823432d8c01598ab3dc3f15cbdd69067b98f5a6ddf05c1c4

RUN set -eux; \
    curl -fsSL -o /usr/local/bin/wp \
      "https://github.com/wp-cli/wp-cli/releases/download/v${WP_CLI_VERSION}/wp-cli-${WP_CLI_VERSION}.phar"; \
    echo "${WP_CLI_SHA256}  /usr/local/bin/wp" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/wp; \
    mkdir -p /usr/src/store /usr/local/share/store-health; \
    curl -fsSL -o /usr/src/store/woocommerce.zip \
      "https://downloads.wordpress.org/plugin/woocommerce.${WOOCOMMERCE_VERSION}.zip"; \
    echo "${WOOCOMMERCE_SHA256}  /usr/src/store/woocommerce.zip" | sha256sum -c -; \
    curl -fsSL -o /usr/src/store/storefront.zip \
      "https://downloads.wordpress.org/theme/storefront.${STOREFRONT_VERSION}.zip"; \
    echo "${STOREFRONT_SHA256}  /usr/src/store/storefront.zip" | sha256sum -c -; \
    # Core bundles plugins and themes a store does not use. One default theme
    # stays as WordPress's fallback if the active theme ever fails to load.
    rm -rf /usr/src/wordpress/wp-content/plugins/akismet \
           /usr/src/wordpress/wp-content/plugins/hello.php \
           /usr/src/wordpress/wp-content/themes/twentytwentythree \
           /usr/src/wordpress/wp-content/themes/twentytwentyfour; \
    # This image brings its own wp-config.php; drop the unused templates.
    rm -f /usr/src/wordpress/wp-config-docker.php /usr/src/wordpress/wp-config-sample.php; \
    echo ok > /usr/local/share/store-health/healthz

# The setup script reads these to decide whether the volume needs an upgrade.
ENV WOOCOMMERCE_VERSION=${WOOCOMMERCE_VERSION} \
    STOREFRONT_VERSION=${STOREFRONT_VERSION} \
    WP_CLI_CACHE_DIR=/tmp/wp-cli-cache

COPY apache/ports.conf /etc/apache2/ports.conf
COPY apache/000-default.conf /etc/apache2/sites-available/000-default.conf
COPY apache/store.conf /etc/apache2/conf-available/store.conf
COPY php/store.ini /usr/local/etc/php/conf.d/zz-store.ini
COPY --chown=www-data:www-data wp-config.php /usr/src/wordpress/wp-config.php
COPY setup/ /usr/local/share/store-setup/
COPY --chmod=0755 bin/store-setup /usr/local/bin/store-setup

# Copied files keep whatever permissions they had in the checkout, and those
# depend on the umask of whoever cloned the repository. With a strict one the
# files arrive readable by root only, and Apache, which runs as www-data,
# cannot open its own configuration. So readability is set here, not inherited.
RUN chmod -R a+rX /etc/apache2/ports.conf /etc/apache2/sites-available/000-default.conf \
      /etc/apache2/conf-available/store.conf /usr/local/etc/php/conf.d/zz-store.ini \
      /usr/src/wordpress/wp-config.php /usr/local/share/store-setup \
    && a2enconf store

USER www-data
EXPOSE 8080
