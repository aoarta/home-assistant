#!/bin/bash


echo "Fetch roof data"
python horizon_from_roof.py

echo ENTER
read x

gwenview horizon_plot_roof.png

echo ENTER
read x

python horizon_mask_gen.py horizon_profile_roof.csv
mv horizon_mask.jinja horizon_mask_roof.jinja


echo "Fetch balcony data"
python horizon_from_balcony.py

echo ENTER
read x

gwenview horizon_plot_balcony.png

echo ENTER
read x


python horizon_mask_gen.py horizon_profile_balcony.csv
mv horizon_mask.jinja horizon_mask_balcony.jinja



echo "upload horizon_mask_jinja manually!!!!"
