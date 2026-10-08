<?php
/**
 * WordPress configuration for a store pod.
 *
 * Nothing in this file belongs to one particular store. Every per-store value
 * and every secret arrives through the environment, which Kubernetes fills from
 * Helm values and from the store's Secret.
 */

if ( ! function_exists( 'store_env' ) ) {
	/**
	 * Reads an environment variable. Without a default the variable is
	 * required, and a missing one stops the request instead of letting
	 * WordPress start half-configured.
	 */
	function store_env( string $name, ?string $default = null ): string {
		$value = getenv( $name );
		if ( false !== $value && '' !== $value ) {
			return $value;
		}
		if ( null !== $default ) {
			return $default;
		}
		error_log( "wp-config: required environment variable {$name} is not set" );
		if ( PHP_SAPI !== 'cli' ) {
			http_response_code( 500 );
		}
		exit( 1 );
	}
}

define( 'DB_HOST', store_env( 'WORDPRESS_DB_HOST' ) );
define( 'DB_NAME', store_env( 'WORDPRESS_DB_NAME' ) );
define( 'DB_USER', store_env( 'WORDPRESS_DB_USER' ) );
define( 'DB_PASSWORD', store_env( 'WORDPRESS_DB_PASSWORD' ) );
define( 'DB_CHARSET', 'utf8mb4' );
define( 'DB_COLLATE', '' );
$table_prefix = 'wp_';

// Keys and salts sign login cookies and nonces. They come from the Secret so
// that they stay the same across pod restarts; generating them at start would
// log every user out whenever the pod is replaced.
foreach ( array( 'AUTH_KEY', 'SECURE_AUTH_KEY', 'LOGGED_IN_KEY', 'NONCE_KEY', 'AUTH_SALT', 'SECURE_AUTH_SALT', 'LOGGED_IN_SALT', 'NONCE_SALT' ) as $store_key ) {
	define( $store_key, store_env( "WORDPRESS_{$store_key}" ) );
}
unset( $store_key );

// The public URL is derived from the Ingress host, so the two cannot drift.
define( 'WP_HOME', rtrim( store_env( 'STORE_URL' ), '/' ) );
define( 'WP_SITEURL', WP_HOME );

// TLS ends at the ingress controller, which reports the original scheme here.
if ( isset( $_SERVER['HTTP_X_FORWARDED_PROTO'] ) && 'https' === $_SERVER['HTTP_X_FORWARDED_PROTO'] ) {
	$_SERVER['HTTPS'] = 'on';
}

// Core ships in the image and the web root is rebuilt on every pod start, so a
// running pod must not update or edit code.
define( 'WP_AUTO_UPDATE_CORE', false );
define( 'DISALLOW_FILE_EDIT', true );

// Scheduled work is triggered by a Kubernetes CronJob rather than by whichever
// visitor happens to load a page.
define( 'DISABLE_WP_CRON', true );

define( 'WP_DEBUG', '1' === store_env( 'WORDPRESS_DEBUG', '0' ) );
define( 'WP_DEBUG_DISPLAY', false );
define( 'WP_ENVIRONMENT_TYPE', store_env( 'WP_ENVIRONMENT_TYPE', 'production' ) );

if ( ! defined( 'ABSPATH' ) ) {
	define( 'ABSPATH', __DIR__ . '/' );
}

require_once ABSPATH . 'wp-settings.php';
