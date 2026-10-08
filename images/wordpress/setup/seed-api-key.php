<?php
/**
 * Registers the WooCommerce REST API key that the MCP server signs requests with.
 *
 * The key pair is generated outside WordPress (in the store's Kubernetes Secret)
 * and handed to both sides, so nothing ever has to read a key back out of
 * WooCommerce. WooCommerce keeps only an HMAC of the consumer key, exactly as it
 * does for keys created in wp-admin.
 *
 * Re-running updates the existing row, which is how a rotated Secret takes
 * effect on the next pod start.
 *
 * Run with: wp eval-file seed-api-key.php
 */

$consumer_key    = getenv( 'WC_CONSUMER_KEY' );
$consumer_secret = getenv( 'WC_CONSUMER_SECRET' );
$login           = getenv( 'WC_API_USER' ) ?: 'mcp-bot';
$description     = 'woo-mcp (managed by the platform)';

if ( ! $consumer_key || ! $consumer_secret ) {
	WP_CLI::error( 'WC_CONSUMER_KEY and WC_CONSUMER_SECRET must be set.' );
}
if ( ! function_exists( 'wc_api_hash' ) ) {
	WP_CLI::error( 'WooCommerce is not active.' );
}

// A dedicated account owns the key, so API calls run as a shop manager rather
// than as the site administrator. Nobody logs in as this user: its password is
// random and discarded.
$user = get_user_by( 'login', $login );
if ( $user ) {
	$user_id = $user->ID;
} else {
	$user_id = wp_insert_user(
		array(
			'user_login'   => $login,
			'user_pass'    => wp_generate_password( 48, true, true ),
			'user_email'   => $login . '@noreply.invalid',
			'display_name' => 'MCP server',
			'role'         => 'shop_manager',
		)
	);
	if ( is_wp_error( $user_id ) ) {
		WP_CLI::error( $user_id );
	}
}

global $wpdb;
$table = $wpdb->prefix . 'woocommerce_api_keys';
$row   = array(
	'user_id'         => $user_id,
	'description'     => $description,
	'permissions'     => 'read_write',
	'consumer_key'    => wc_api_hash( $consumer_key ),
	'consumer_secret' => $consumer_secret,
	'truncated_key'   => substr( $consumer_key, -7 ),
);

$key_id = $wpdb->get_var( $wpdb->prepare( "SELECT key_id FROM {$table} WHERE description = %s", $description ) );
$result = $key_id
	? $wpdb->update( $table, $row, array( 'key_id' => $key_id ) )
	: $wpdb->insert( $table, $row );

if ( false === $result ) {
	WP_CLI::error( 'Could not write the API key: ' . $wpdb->last_error );
}
WP_CLI::success( sprintf( 'API key ...%s registered for user %s.', $row['truncated_key'], $login ) );
