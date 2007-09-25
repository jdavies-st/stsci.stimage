/*
    Program:    computeMean.c
    Author:     Christopher Hanley
    Purpose:    Compute the mean, stddev, max, and min for a nnumarray object
                while applying some upper and lower pixel clipping values.
*/

#include <Python.h>
#include "numpy/arrayobject.h"


#include <string.h>
#include <stdio.h>
#include <math.h>


int computeMean_(double *image, int nelements, double clipmin, double clipmax, 
                               int *numGoodPixels, double *mean, double *stddev, 
                                double *minValue, double *maxValue)
{
    int i;
    double tmpMinValue, tmpMaxValue;
    double sum, sumsq;

    /* Initialize some local variables */
    sum = 0;
    sumsq = 0;

    /*Initialize the tmpMinValue and tmpMaxValue so that we can find the 
      largest and smallest non-clipped values */

    tmpMinValue = clipmax;
    tmpMaxValue = clipmin;

    for (i = 0; i < nelements; i++) {
        if ( (image[i] >= clipmin) && (image[i] <= clipmax) ) {
            /* Find lowest value in the clipped image */
            if (image[i] < tmpMinValue) {
                tmpMinValue = image[i];
            }

            /* Find largest value in the clipped image */
            if (image[i] > tmpMaxValue) {
                tmpMaxValue = image[i];
            }

            /* Increment the counter of of numGoodValues (i.e. not clipped) */
            *numGoodPixels = *numGoodPixels + 1;

            /* Compute the sum of the "good" pixels */
            sum = sum + image[i];

            /* Compute the sum of the pixels squared */
            sumsq = sumsq + (image[i] * image[i]);
        }
    }

    *minValue = tmpMinValue;
    *maxValue = tmpMaxValue;
    *mean = (double)(sum / *numGoodPixels);
    *stddev = (double)sqrt((sumsq - (*mean * sum)) / (*numGoodPixels - 1));

    return 1;
}

static PyObject * computeMean(PyObject *obj, PyObject *args)
{
    PyObject *oimage;
    PyArrayObject *image;
    int status=0;
    int numGoodPixels;
    double clipmin, clipmax, mean, stddev, minValue, maxValue;

    if (!PyArg_ParseTuple(args,"Off:computeMean",&oimage, &clipmin, &clipmax))
	    return NULL;

    image = (PyArrayObject *)PyArray_ContiguousFromObject(oimage, PyArray_DOUBLE, 1, 2);

    if (!image) return NULL;

    mean = 0;
    stddev = 0;
    numGoodPixels = 0;
    minValue = 0;
    maxValue = 0;

    status = computeMean_((double *)image->data, PyArray_Size((PyObject*)image), 
			  clipmin, clipmax,
			  &numGoodPixels, &mean, &stddev, &minValue, &maxValue);
    Py_XDECREF(image); 

    return Py_BuildValue("idddd",numGoodPixels,mean,stddev,minValue,maxValue);
}

static PyMethodDef computeMean_methods[] =
{
    {"computeMean",  computeMean, METH_VARARGS, 
        "computeMean(image, clipmin, clipmax, numGoodPixels, mean, stddev, minValue, maxValue)"},
    {0,            0}                             /* sentinel */
};

void initcomputeMean(void) {
	Py_InitModule("computeMean", computeMean_methods);
    import_array();
}
