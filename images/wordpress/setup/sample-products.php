<?php
/**
 * Creates a few sample products so a new storefront is not empty.
 *
 * Each product is identified by its SKU, so running this twice creates nothing
 * the second time.
 *
 * Run with: wp eval-file sample-products.php
 */

$samples = array(
	array( 'sample-tee', 'Classic T-Shirt', '19.99', 'A soft cotton tee that goes with everything.' ),
	array( 'sample-tote', 'Canvas Tote Bag', '14.50', 'A sturdy tote for groceries, books or the beach.' ),
	array( 'sample-mug', 'Ceramic Mug', '9.00', 'Holds 350 ml of whatever gets you through the morning.' ),
);

foreach ( $samples as list( $sku, $name, $price, $description ) ) {
	if ( wc_get_product_id_by_sku( $sku ) ) {
		continue;
	}
	$product = new WC_Product_Simple();
	$product->set_sku( $sku );
	$product->set_name( $name );
	$product->set_regular_price( $price );
	$product->set_short_description( $description );
	$product->set_status( 'publish' );
	$product->save();
	WP_CLI::log( "Created sample product: {$name}" );
}
