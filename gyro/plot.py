# this program plots the data from the csv file on a xy scatter plot with a linear trend line

import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns

def plot_data(file_path):
    # Read the data from the csv file
    data = pd.read_csv(file_path)

    # Set the style of seaborn
    sns.set_theme(style="whitegrid")

    # Create a scatter plot
    plt.figure(figsize=(10, 6))
    plt.scatter(data['Temp'], data['Drift'], color='blue', label='Data Points')

    # Fit a linear regression line to the data
    m, b = np.polyfit(data['Temp'], data['Drift'], 1)
    plt.plot(data['Temp'], m * data['Temp'] + b, color='red', label='Trend Line')

    # Print the equation of the trend line
    print(f"Trend Line Equation: Drift = {m:.6f} * Temp + {b:.6f}")

    # Display the equation on the plot
    equation_text = f"Drift = {m:.6f} * Temp + {b:.6f}"
    plt.text(0.05, 0.95, equation_text, transform=plt.gca().transAxes, fontsize=12, verticalalignment='top', color='red')

    # Add labels and title
    plt.xlabel('Temperature (°C)')
    plt.ylabel('Drift')
    plt.title('Scatter Plot of Drift vs Temperature with Trend Line')
    plt.legend()

    # Show the plot
    plt.show()

def plot_averaged_data(file_path, chunk_size=10):
    """
    This function reads the data from a csv file, averages the data in chunks of size `chunk_size`,
    and plots the averaged data with a linear trend line.
    """
    # Read the data from the csv file
    data = pd.read_csv(file_path)

    # Group data into chunks of 10 and calculate the mean for each chunk
    averaged_data = data.groupby(data.index // chunk_size).mean()

    # Set the style of seaborn
    sns.set_theme(style="whitegrid")

    # Create a scatter plot for averaged data
    plt.figure(figsize=(10, 6))
    plt.scatter(averaged_data['Temp'], averaged_data['Drift'], color='green', label='Averaged Data Points')

    # Fit a linear regression line to the averaged data
    m, b = np.polyfit(averaged_data['Temp'], averaged_data['Drift'], 1)
    plt.plot(averaged_data['Temp'], m * averaged_data['Temp'] + b, color='orange', label='Trend Line (Averaged)')

    # Print the equation of the trend line for averaged data
    print(f"Averaged Trend Line Equation: Drift = {m:.6f} * Temp + {b:.6f}")

    # Display the equation on the plot
    equation_text = f"Drift = {m:.6f} * Temp + {b:.6f}"
    plt.text(0.05, 0.95, equation_text, transform=plt.gca().transAxes, fontsize=12, verticalalignment='top', color='orange')

    # Add labels and title
    plt.xlabel('Temperature (°C)')
    plt.ylabel('Drift')
    plt.title('Averaged Scatter Plot of Drift vs Temperature with Trend Line')
    plt.legend()

    # Show the plot
    plt.show()

def plot_combined_data(file_path1, file_path2, file_path3, file_path4, file_path5):
    # Read the data from the five csv files
    data1 = pd.read_csv(file_path1)
    data2 = pd.read_csv(file_path2)
    data3 = pd.read_csv(file_path3)
    data4 = pd.read_csv(file_path4)
    data5 = pd.read_csv(file_path5)

    # Set the style of seaborn
    sns.set_theme(style="whitegrid")

    # Create a scatter plot for the first dataset
    plt.figure(figsize=(10, 6))
    plt.scatter(data1['Temp'], data1['Drift'], color='blue', label='Dataset 1')
    m1, b1 = np.polyfit(data1['Temp'], data1['Drift'], 1)
    plt.plot(data1['Temp'], m1 * data1['Temp'] + b1, color='red', label='Trend Line (Dataset 1)')
    print(f"Dataset 1 Trend Line Equation: Drift = {m1:.6f} * Temp + {b1:.6f}")

    # Create a scatter plot for the second dataset
    plt.scatter(data2['Temp'], data2['Drift'], color='green', label='Dataset 2')
    m2, b2 = np.polyfit(data2['Temp'], data2['Drift'], 1)
    plt.plot(data2['Temp'], m2 * data2['Temp'] + b2, color='orange', label='Trend Line (Dataset 2)')
    print(f"Dataset 2 Trend Line Equation: Drift = {m2:.6f} * Temp + {b2:.6f}")

    # Create a scatter plot for the third dataset
    plt.scatter(data3['Temp'], data3['Drift'], color='purple', label='Dataset 3')
    m3, b3 = np.polyfit(data3['Temp'], data3['Drift'], 1)
    plt.plot(data3['Temp'], m3 * data3['Temp'] + b3, color='pink', label='Trend Line (Dataset 3)')
    print(f"Dataset 3 Trend Line Equation: Drift = {m3:.6f} * Temp + {b3:.6f}")

    # Create a scatter plot for the fourth dataset
    plt.scatter(data4['Temp'], data4['Drift'], color='cyan', label='Dataset 4')
    m4, b4 = np.polyfit(data4['Temp'], data4['Drift'], 1)
    plt.plot(data4['Temp'], m4 * data4['Temp'] + b4, color='darkcyan', label='Trend Line (Dataset 4)')
    print(f"Dataset 4 Trend Line Equation: Drift = {m4:.6f} * Temp + {b4:.6f}")

    # Create a scatter plot for the fifth dataset
    plt.scatter(data5['Temp'], data5['Drift'], color='brown', label='Dataset 5')
    m5, b5 = np.polyfit(data5['Temp'], data5['Drift'], 1)
    plt.plot(data5['Temp'], m5 * data5['Temp'] + b5, color='darkred', label='Trend Line (Dataset 5)')
    print(f"Dataset 5 Trend Line Equation: Drift = {m5:.6f} * Temp + {b5:.6f}")

    # Add labels and title
    plt.xlabel('Temperature (°C)')
    plt.ylabel('Drift')
    plt.title('Scatter Plot of Drift vs Temperature for Multiple Datasets')
    plt.legend()

    # Show the plot
    plt.show()

if __name__ == "__main__":
    # Example usage
    file_path1 = 'Data Gyro Drift.txt'  # First dataset
    file_path2 = 'Data Gyro Drift-new2.txt'  # Second dataset
    file_path3 = 'Data Gyro Drift-new3.txt'  # Third dataset
    file_path4 = 'Data Gyro Drift-new4.txt'  # Fourth dataset
    file_path5 = 'Data Gyro Drift-new5.txt'  # Fifth dataset
    plot_combined_data(file_path1, file_path2, file_path3, file_path4, file_path5)