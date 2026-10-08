<?php
/**
 * Sets the administrator's password from the environment.
 *
 * WP-CLI takes a password either as a command-line argument or through
 * --prompt, and it prints prompted values to the log. Reading the environment
 * here keeps the password out of the pod's logs and out of the process list.
 *
 * Run with: wp eval-file set-admin-password.php
 */

$login    = getenv( 'WP_ADMIN_USER' );
$password = getenv( 'WP_ADMIN_PASSWORD' );

if ( ! $login || ! $password ) {
	WP_CLI::error( 'WP_ADMIN_USER and WP_ADMIN_PASSWORD must be set.' );
}
$user = get_user_by( 'login', $login );
if ( ! $user ) {
	WP_CLI::error( "No such user: {$login}" );
}

wp_set_password( $password, $user->ID );
WP_CLI::success( "Password set for {$login}." );
